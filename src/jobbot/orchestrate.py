"""Driving the arms: one sequence, one run history, one lock.

The arms remain independent — nothing here lets one arm call another, and every
handoff is still a row transition. What this module adds is the part that was
missing: something that knows the order, records that a run happened, and can
answer "is arm 1 due yet" without a human keeping track.

Three decisions are worth stating because they are not obvious from the code:

* **Preflight runs for every requested arm before any arm executes.** Arm 1 takes
  twenty minutes against a real registry. Discovering only afterwards that Ollama
  is not running, and that arm 3 was therefore never going to work, wastes the
  whole run to learn something knowable at the start.
* **An arm failing does not end the run.** The arms are independent by
  construction, so arm 2 failing has no bearing on arm 3's ability to draft from
  what is already stored. Failures are recorded, the remaining arms run, and the
  exit code is non-zero. ``--stop-on-error`` opts into the strict order.
* **Cadence lives here, not in the scheduler.** An external scheduler fires
  ``run --due-only`` on a fixed short interval and this decides what is actually
  due, so there is exactly one place that knows how often arm 1 should poll.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from jobbot.config import Settings, get_settings
from jobbot.db import session_scope, sync_schema
from jobbot.models import PipelineRun, RunStatus, as_utc

logger = logging.getLogger("jobbot.orchestrate")


class PreflightError(RuntimeError):
    """An arm cannot run with the current configuration or data."""

    def __init__(self, arm: int, reason: str) -> None:
        super().__init__(reason)
        self.arm = arm
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ArmOutcome:
    """What an arm did, in a shape the driver can store and print.

    The four arms return four unrelated report dataclasses. Rather than teach the
    driver about all of them, each adapter flattens its report into display
    labels here — which is also what goes into ``PipelineRun.metrics``, so the
    stored history reads the same as the terminal output did.
    """

    metrics: dict[str, int] = field(default_factory=dict)
    detail: dict[str, int] = field(default_factory=dict)
    detail_title: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ArmSpec:
    number: int
    name: str
    description: str
    interval_setting: str
    preflight: Callable[[Settings], None]
    runner: Callable[[Settings], ArmOutcome]

    def label(self) -> str:
        return f"arm {self.number} ({self.name})"

    def interval(self, settings: Settings) -> timedelta:
        return timedelta(hours=getattr(settings, self.interval_setting))


@dataclass(frozen=True, slots=True)
class ArmResult:
    spec: ArmSpec
    outcome: ArmOutcome | None
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def ran(self) -> bool:
        return self.outcome is not None

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    results: list[ArmResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(result.failed for result in self.results)

    @property
    def ran_anything(self) -> bool:
        return any(result.ran for result in self.results)


# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------


def _check_profile(arm: int) -> None:
    from jobbot.profile import ProfileNotFoundError, get_profile

    try:
        get_profile()
    except ProfileNotFoundError as error:
        raise PreflightError(arm, str(error)) from None


def _preflight_companies(settings: Settings) -> None:
    # GitHub organisation search needs no token — only a token-less rate limit —
    # so this arm always has at least one working source.
    return None


def _preflight_discovery(settings: Settings) -> None:
    from jobbot.connectors.boards import load_boards

    _check_profile(1)
    if not load_boards():
        raise PreflightError(1, "no boards configured; see data/boards.json")


def _preflight_contact(settings: Settings) -> None:
    from jobbot.arms import contact

    if contact.load_seed():
        return
    # An empty seed file is fine once arm 0 has found companies of its own —
    # requiring the file would make the discovery arm pointless.
    sync_schema()
    with session_scope() as session:
        if contact.pending_companies(session, limit=1):
            return
    raise PreflightError(
        2, "no seed companies; see data/tr_companies.json or run 'jobbot companies discover'"
    )


def _preflight_apply(settings: Settings) -> None:
    from jobbot.llm import LlmUnavailableError, OllamaClient

    _check_profile(3)
    with OllamaClient(settings) as client:
        try:
            client.health()
        except LlmUnavailableError as error:
            raise PreflightError(3, str(error)) from None


# --------------------------------------------------------------------------
# Arm adapters
# --------------------------------------------------------------------------


def _run_companies(settings: Settings) -> ArmOutcome:
    import asyncio

    from jobbot.arms import companies

    sync_schema()
    with session_scope() as session:
        report = asyncio.run(companies.run(session=session, settings=settings))

    return ArmOutcome(
        metrics={
            "companies found": report.found,
            "new companies": report.added,
            "already known": report.already_known,
        },
        detail=dict(report.per_source),
        detail_title="companies per source",
        hint="run 'jobbot run --arm 2' to visit them" if report.added else "",
    )


def _run_discovery(settings: Settings) -> ArmOutcome:
    import asyncio

    from jobbot.arms import discover
    from jobbot.connectors.ats import build_connectors
    from jobbot.connectors.boards import load_boards

    boards = load_boards()
    sync_schema()
    connectors = build_connectors(boards)
    logger.info("arm 1: %d boards across %d providers", len(boards), len(connectors))

    report = asyncio.run(discover.run(connectors, settings=settings))

    return ArmOutcome(
        metrics={
            "fetched": report.fetched,
            "duplicates dropped": report.duplicates,
            "rejected by rules": report.rejected,
            "below match threshold": report.below_threshold,
            "already known": report.already_known,
            "stored": report.stored,
        },
        detail=dict(report.reject_reasons),
        detail_title="why postings were rejected",
    )


def _run_contact(settings: Settings) -> ArmOutcome:
    import asyncio

    from jobbot.arms import contact

    sync_schema()
    with session_scope() as session:
        report = asyncio.run(
            contact.run(settings=settings, session=session, limit=settings.arm2_batch_size)
        )

    return ArmOutcome(
        metrics={
            "companies visited": report.processed,
            "careers pages found": report.careers_pages,
            "ATS boards detected": report.ats_detected,
            "published emails found": report.emails_found,
            "boards added to arm 1": report.boards_added,
        },
        detail=dict(report.outcomes),
        detail_title="outcome per company",
    )


def _run_apply(settings: Settings) -> ArmOutcome:
    from jobbot.llm import OllamaClient
    from jobbot.outbox import build
    from jobbot.profile import get_profile

    sync_schema()
    profile = get_profile()
    limit = settings.arm3_batch_size

    with OllamaClient(settings) as client, session_scope() as session:
        report = build(
            client, profile=profile, session=session, settings=settings, limit=limit
        )

    return ArmOutcome(
        metrics={"applications drafted": report.written},
        detail=dict(report.skipped),
        detail_title="not queued",
        hint=f"start with {report.directory / 'README.md'}" if report.written else "",
    )


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        number=0,
        name="companies",
        description="Find employers nobody wrote down",
        interval_setting="arm0_interval_hours",
        preflight=_preflight_companies,
        runner=_run_companies,
    ),
    ArmSpec(
        number=1,
        name="discover",
        description="Open roles from ATS APIs, scored against your profile",
        interval_setting="arm1_interval_hours",
        preflight=_preflight_discovery,
        runner=_run_discovery,
    ),
    ArmSpec(
        number=2,
        name="contact",
        description="The best reachable application channel per company",
        interval_setting="arm2_interval_hours",
        preflight=_preflight_contact,
        runner=_run_contact,
    ),
    ArmSpec(
        number=3,
        name="apply",
        description="A drafted application per role, written to outbox/",
        interval_setting="arm3_interval_hours",
        preflight=_preflight_apply,
        runner=_run_apply,
    ),
)

BY_NUMBER: dict[int, ArmSpec] = {spec.number: spec for spec in ARMS}


class UnknownArmError(ValueError):
    pass


def arms_for(selector: str) -> list[ArmSpec]:
    """Resolve an ``--arm`` value into the arms to run, in pipeline order."""
    if selector == "all":
        return list(ARMS)
    try:
        number = int(selector)
    except ValueError:
        raise UnknownArmError(selector) from None
    if number not in BY_NUMBER:
        raise UnknownArmError(selector)
    return [BY_NUMBER[number]]


# --------------------------------------------------------------------------
# Run history and cadence
# --------------------------------------------------------------------------


def last_run(session: Session, arm: int, *, status: RunStatus | None = None) -> PipelineRun | None:
    query = select(PipelineRun).where(PipelineRun.arm == arm)
    if status is not None:
        query = query.where(PipelineRun.status == status)
    query = query.order_by(col(PipelineRun.started_at).desc(), col(PipelineRun.id).desc())
    return session.exec(query.limit(1)).first()


def next_due_at(spec: ArmSpec, settings: Settings, last: PipelineRun | None) -> datetime | None:
    """When this arm becomes due. None means "now, it has never run"."""
    if last is None:
        return None
    started = as_utc(last.started_at)
    if started is None:  # pragma: no cover - started_at is never null
        return None
    return started + spec.interval(settings)


def is_due(spec: ArmSpec, settings: Settings, session: Session, now: datetime) -> bool:
    """Has this arm's interval elapsed since it last completed?

    Measured from the last *completed* run, so a failing arm is retried on the
    next tick instead of being locked out for a full interval by its own failure.
    """
    due_at = next_due_at(spec, settings, last_run(session, spec.number, status=RunStatus.COMPLETED))
    return due_at is None or now >= due_at


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _open_run(arm: int) -> int | None:
    with session_scope() as session:
        record = PipelineRun(arm=arm, status=RunStatus.RUNNING, pid=os.getpid())
        session.add(record)
        session.flush()
        return record.id


def _close_run(
    run_id: int | None, *, status: RunStatus, metrics: dict[str, int], error: str | None
) -> None:
    if run_id is None:  # pragma: no cover - only if the insert above failed
        return
    with session_scope() as session:
        record = session.get(PipelineRun, run_id)
        if record is None:  # pragma: no cover
            return
        record.status = status
        record.finished_at = datetime.now(UTC)
        record.metrics = dict(metrics)
        record.error = error
        session.add(record)


def execute(spec: ArmSpec, settings: Settings) -> ArmResult:
    """Run one arm, recording the attempt whether or not it succeeds."""
    sync_schema()
    run_id = _open_run(spec.number)
    try:
        outcome = spec.runner(settings)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        logger.exception("%s failed", spec.label())
        _close_run(run_id, status=RunStatus.FAILED, metrics={}, error=message)
        return ArmResult(spec=spec, outcome=None, error=message)

    _close_run(run_id, status=RunStatus.COMPLETED, metrics=outcome.metrics, error=None)
    return ArmResult(spec=spec, outcome=outcome)


def preflight(specs: Sequence[ArmSpec], settings: Settings) -> list[PreflightError]:
    """Check every arm before running any. Returns the failures, in arm order."""
    failures: list[PreflightError] = []
    for spec in specs:
        try:
            spec.preflight(settings)
        except PreflightError as error:
            failures.append(error)
    return failures


def select_due(
    specs: Sequence[ArmSpec], settings: Settings, *, now: datetime | None = None
) -> tuple[list[ArmSpec], dict[int, datetime]]:
    """Split arms into those due now and the times the rest become due."""
    now = now or datetime.now(UTC)
    sync_schema()
    due: list[ArmSpec] = []
    waiting: dict[int, datetime] = {}
    with session_scope() as session:
        for spec in specs:
            if is_due(spec, settings, session, now):
                due.append(spec)
                continue
            last = last_run(session, spec.number, status=RunStatus.COMPLETED)
            due_at = next_due_at(spec, settings, last)
            if due_at is not None:
                waiting[spec.number] = due_at
    return due, waiting


def run_arms(
    specs: Sequence[ArmSpec],
    settings: Settings | None = None,
    *,
    stop_on_error: bool = False,
    on_start: Callable[[ArmSpec], None] | None = None,
    on_finish: Callable[[ArmResult], None] | None = None,
) -> PipelineResult:
    """Execute arms in order, reporting progress through the callbacks."""
    settings = settings or get_settings()
    results: list[ArmResult] = []

    for index, spec in enumerate(specs):
        if on_start is not None:
            on_start(spec)
        result = execute(spec, settings)
        results.append(result)
        if on_finish is not None:
            on_finish(result)

        if result.failed and stop_on_error:
            for remaining in specs[index + 1 :]:
                results.append(
                    ArmResult(
                        spec=remaining,
                        outcome=None,
                        skipped_reason=f"arm {spec.number} failed",
                    )
                )
            break

    return PipelineResult(results=results)
