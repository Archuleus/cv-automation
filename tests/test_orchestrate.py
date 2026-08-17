"""Driver tests.

Every arm is stubbed. Nothing here touches the network, Ollama or a real board
registry — the point is the sequencing, the recorded history and the cadence
arithmetic, all of which must be right before an unattended run is trusted with
any of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import col, select

from jobbot import db, orchestrate
from jobbot.config import Settings, get_settings
from jobbot.db import session_scope
from jobbot.models import PipelineRun, RunStatus
from jobbot.orchestrate import ArmOutcome, ArmSpec, PreflightError


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path) -> Iterator[None]:
    monkeypatch.setenv("JOBBOT_DATABASE_URL", f"sqlite:///{tmp_path / 'jobbot.db'}")
    get_settings.cache_clear()
    db.reset_engine()
    db.sync_schema()
    yield
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def config() -> Settings:
    return get_settings()


def spec(
    number: int,
    *,
    runner=None,
    preflight=None,
    interval: str = "arm1_interval_hours",
) -> ArmSpec:
    return ArmSpec(
        number=number,
        name=f"stub{number}",
        description=f"stub arm {number}",
        interval_setting=interval,
        preflight=preflight or (lambda settings: None),
        runner=runner or (lambda settings: ArmOutcome(metrics={"did": 1})),
    )


def exploding(message: str = "connector died"):
    def runner(settings):
        raise RuntimeError(message)

    return runner


def refusing(arm: int, reason: str):
    def preflight(settings):
        raise PreflightError(arm, reason)

    return preflight


@dataclass(frozen=True)
class StoredRun:
    """A run row read out of its session, so assertions cannot detach it."""

    arm: int
    status: RunStatus
    metrics: dict
    error: str | None
    pid: int | None
    finished: bool
    duration_seconds: float | None


def stored_runs() -> list[StoredRun]:
    with session_scope() as session:
        rows = session.exec(
            select(PipelineRun).order_by(col(PipelineRun.id))
        ).all()
        return [
            StoredRun(
                arm=row.arm,
                status=row.status,
                metrics=dict(row.metrics),
                error=row.error,
                pid=row.pid,
                finished=row.finished_at is not None,
                duration_seconds=row.duration_seconds,
            )
            for row in rows
        ]


class TestArmSelection:
    def test_all_returns_every_arm_in_pipeline_order(self):
        assert [s.number for s in orchestrate.arms_for("all")] == [0, 1, 2, 3]

    def test_a_single_arm_can_be_selected(self):
        assert [s.number for s in orchestrate.arms_for("2")] == [2]

    def test_arm_zero_is_selectable(self):
        # Arm 0 predates the driver but was never reachable from `run`.
        assert [s.number for s in orchestrate.arms_for("0")] == [0]

    def test_an_out_of_range_arm_is_rejected(self):
        with pytest.raises(orchestrate.UnknownArmError):
            orchestrate.arms_for("9")

    def test_a_non_numeric_arm_is_rejected(self):
        with pytest.raises(orchestrate.UnknownArmError):
            orchestrate.arms_for("discovery")

    def test_every_real_arm_declares_an_interval_setting(self, config):
        for arm in orchestrate.ARMS:
            assert isinstance(getattr(config, arm.interval_setting), int)


class TestPreflight:
    def test_passes_when_no_arm_objects(self, config):
        assert orchestrate.preflight([spec(1), spec(2)], config) == []

    def test_collects_every_failure_rather_than_the_first(self, config):
        # Reporting one failure at a time turns a misconfigured run into several
        # round trips, which is exactly what running preflight up front avoids.
        failures = orchestrate.preflight(
            [
                spec(1, preflight=refusing(1, "no boards")),
                spec(2),
                spec(3, preflight=refusing(3, "ollama down")),
            ],
            config,
        )

        assert [f.arm for f in failures] == [1, 3]
        assert failures[0].reason == "no boards"
        assert failures[1].reason == "ollama down"


class TestContactPreflight:
    """Arm 2's own preflight, which is the one that changed shape in phase 5."""

    @pytest.fixture
    def empty_seed(self, monkeypatch, tmp_path) -> Settings:
        seed = tmp_path / "tr_companies.json"
        seed.write_text('{"companies": []}', encoding="utf-8")
        monkeypatch.setenv("JOBBOT_TR_COMPANIES_PATH", str(seed))
        get_settings.cache_clear()
        return get_settings()

    def test_refuses_when_there_is_no_seed_and_nothing_discovered(self, empty_seed):
        with pytest.raises(PreflightError) as raised:
            orchestrate._preflight_contact(empty_seed)

        assert raised.value.arm == 2
        assert "companies discover" in raised.value.reason

    def test_accepts_discovered_companies_with_an_empty_seed_file(self, empty_seed):
        # The point of arm 0: requiring the seed file would make discovery
        # pointless, since a company it finds is only ever in the database.
        from jobbot.models import Company

        with session_scope() as session:
            session.add(Company(name="Acme Yazilim", domain="acme.com.tr", country="TR"))

        assert orchestrate._preflight_contact(empty_seed) is None

    def test_ignores_companies_already_visited(self, empty_seed):
        from jobbot.models import Company

        with session_scope() as session:
            session.add(
                Company(
                    name="Acme Yazilim",
                    domain="acme.com.tr",
                    investigated_at=datetime.now(UTC),
                )
            )

        with pytest.raises(PreflightError):
            orchestrate._preflight_contact(empty_seed)

    def test_company_discovery_needs_no_configuration(self, config):
        # GitHub organisation search works without a token, so arm 0 always has
        # at least one live source and nothing to check up front.
        assert orchestrate._preflight_companies(config) is None


class TestExecute:
    def test_records_a_completed_run_with_its_metrics(self, config):
        result = orchestrate.execute(
            spec(1, runner=lambda settings: ArmOutcome(metrics={"stored": 7})), config
        )

        assert result.ran
        assert not result.failed
        runs = stored_runs()
        assert len(runs) == 1
        assert runs[0].arm == 1
        assert runs[0].status is RunStatus.COMPLETED
        assert runs[0].metrics == {"stored": 7}
        assert runs[0].finished
        assert runs[0].error is None

    def test_records_the_pid_that_ran_it(self, config):
        import os

        orchestrate.execute(spec(1), config)

        assert stored_runs()[0].pid == os.getpid()

    def test_a_failing_arm_is_recorded_rather_than_raised(self, config):
        result = orchestrate.execute(spec(1, runner=exploding("boards.json is gibberish")), config)

        assert result.failed
        assert not result.ran
        assert "boards.json is gibberish" in (result.error or "")
        run = stored_runs()[0]
        assert run.status is RunStatus.FAILED
        assert "RuntimeError" in (run.error or "")
        assert run.finished

    def test_duration_is_derivable_from_the_stored_run(self, config):
        orchestrate.execute(spec(1), config)

        duration = stored_runs()[0].duration_seconds
        assert duration is not None
        assert duration >= 0


class TestRunArms:
    def test_runs_arms_in_the_order_given(self, config):
        order: list[int] = []

        def tracked(number: int) -> ArmSpec:
            return spec(
                number,
                runner=lambda settings, n=number: (order.append(n), ArmOutcome())[1],
            )

        orchestrate.run_arms([tracked(0), tracked(1), tracked(2)], config)

        assert order == [0, 1, 2]

    def test_a_failing_arm_does_not_stop_the_others(self, config):
        # The arms are independent by construction: arm 2 failing has no bearing
        # on arm 3's ability to draft from what is already stored.
        result = orchestrate.run_arms(
            [spec(1, runner=exploding()), spec(2), spec(3)], config
        )

        assert result.failed
        assert [r.spec.number for r in result.results if r.ran] == [2, 3]

    def test_stop_on_error_skips_the_remaining_arms(self, config):
        result = orchestrate.run_arms(
            [spec(1, runner=exploding()), spec(2), spec(3)], config, stop_on_error=True
        )

        assert [r.spec.number for r in result.results] == [1, 2, 3]
        assert [r.spec.number for r in result.results if r.ran] == []
        skipped = [r for r in result.results if r.skipped_reason]
        assert [r.spec.number for r in skipped] == [2, 3]
        assert skipped[0].skipped_reason == "arm 1 failed"

    def test_skipped_arms_leave_no_run_history(self, config):
        orchestrate.run_arms(
            [spec(1, runner=exploding()), spec(2)], config, stop_on_error=True
        )

        assert [run.arm for run in stored_runs()] == [1]

    def test_a_clean_run_is_not_marked_failed(self, config):
        result = orchestrate.run_arms([spec(1), spec(2)], config)

        assert not result.failed
        assert result.ran_anything

    def test_callbacks_report_each_arm(self, config):
        started: list[int] = []
        finished: list[int] = []

        orchestrate.run_arms(
            [spec(1), spec(2)],
            config,
            on_start=lambda s: started.append(s.number),
            on_finish=lambda r: finished.append(r.spec.number),
        )

        assert started == [1, 2]
        assert finished == [1, 2]


class TestCadence:
    def test_an_arm_that_never_ran_is_due(self, config):
        due, waiting = orchestrate.select_due([spec(1)], config)

        assert [s.number for s in due] == [1]
        assert waiting == {}

    def test_an_arm_that_just_ran_is_not_due(self, config):
        orchestrate.execute(spec(1), config)

        due, waiting = orchestrate.select_due([spec(1)], config)

        assert due == []
        assert 1 in waiting

    def test_an_arm_becomes_due_once_its_interval_elapses(self, config):
        orchestrate.execute(spec(1), config)
        later = datetime.now(UTC) + timedelta(hours=config.arm1_interval_hours, minutes=1)

        due, _ = orchestrate.select_due([spec(1)], config, now=later)

        assert [s.number for s in due] == [1]

    def test_the_wait_time_reported_is_one_interval_after_the_last_run(self, config):
        orchestrate.execute(spec(1), config)

        _, waiting = orchestrate.select_due([spec(1)], config)

        expected = datetime.now(UTC) + timedelta(hours=config.arm1_interval_hours)
        assert abs((waiting[1] - expected).total_seconds()) < 120

    def test_a_failed_run_does_not_satisfy_the_interval(self, config):
        # Otherwise an arm that fails locks itself out for a full day, and the
        # hourly tick that would have retried it does nothing.
        orchestrate.execute(spec(1, runner=exploding()), config)

        due, _ = orchestrate.select_due([spec(1)], config)

        assert [s.number for s in due] == [1]

    def test_each_arm_honours_its_own_interval(self, config):
        weekly = spec(0, interval="arm0_interval_hours")
        daily = spec(1, interval="arm1_interval_hours")
        orchestrate.execute(weekly, config)
        orchestrate.execute(daily, config)
        two_days_on = datetime.now(UTC) + timedelta(hours=48)

        due, waiting = orchestrate.select_due([weekly, daily], config, now=two_days_on)

        assert [s.number for s in due] == [1]
        assert set(waiting) == {0}

    def test_arms_are_independent_in_the_history(self, config):
        orchestrate.execute(spec(1), config)

        due, _ = orchestrate.select_due([spec(1), spec(2)], config)

        assert [s.number for s in due] == [2]

    def test_the_newest_completed_run_decides(self, config):
        # A failure after a success must not make the arm look freshly completed,
        # nor hide the success that did happen.
        orchestrate.execute(spec(1), config)
        orchestrate.execute(spec(1, runner=exploding()), config)

        due, waiting = orchestrate.select_due([spec(1)], config)

        assert due == []
        assert 1 in waiting


class TestLastRun:
    def test_returns_none_for_an_arm_with_no_history(self, config):
        with session_scope() as session:
            assert orchestrate.last_run(session, 1) is None

    def test_returns_the_most_recent_attempt(self, config):
        orchestrate.execute(spec(1, runner=lambda s: ArmOutcome(metrics={"n": 1})), config)
        orchestrate.execute(spec(1, runner=lambda s: ArmOutcome(metrics={"n": 2})), config)

        with session_scope() as session:
            latest = orchestrate.last_run(session, 1)
            assert latest is not None
            assert latest.metrics == {"n": 2}

    def test_can_filter_to_completed_runs(self, config):
        orchestrate.execute(spec(1, runner=lambda s: ArmOutcome(metrics={"n": 1})), config)
        orchestrate.execute(spec(1, runner=exploding()), config)

        with session_scope() as session:
            latest = orchestrate.last_run(session, 1, status=RunStatus.COMPLETED)
            assert latest is not None
            assert latest.metrics == {"n": 1}
