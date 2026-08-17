"""Single-instance locking for pipeline runs.

Two overlapping runs are not a correctness problem — ``uq_application_company_period``
rejects a duplicate application whatever the caller does — but they are a waste
problem. Arm 3 spends about a minute of GPU per draft, and two processes racing
to draft the same queue burn that twice to produce one usable card.

Staleness is decided by age, not by asking the operating system whether the
recorded pid is alive. Checking pid liveness portably is not possible here:
``os.kill(pid, 0)`` is the POSIX answer and does not carry to Windows, which is
this project's primary platform, and a liveness check that is wrong in either
direction is worse than no check — a false "alive" locks the pipeline forever, a
false "dead" is the double-run this module exists to prevent. So a lock older
than ``max_age_hours`` is assumed abandoned and reclaimed, and anything younger
is reported with the pid and the path so a person can decide.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("jobbot.locks")


class LockHeldError(RuntimeError):
    """Raised when another run holds the lock and it is not old enough to reclaim."""


@dataclass(frozen=True, slots=True)
class LockInfo:
    """What a lock file says about the run that holds it."""

    pid: int | None
    started_at: datetime | None

    @property
    def age(self) -> timedelta | None:
        if self.started_at is None:
            return None
        return datetime.now(UTC) - self.started_at

    def describe(self) -> str:
        pid = self.pid if self.pid is not None else "unknown"
        age = self.age
        if age is None:
            return f"pid {pid}, started at an unknown time"
        return f"pid {pid}, running for {_humanise(age)}"


def _humanise(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def read_lock(path: Path) -> LockInfo:
    """Describe an existing lock file, tolerating one that is corrupt or partial.

    A lock file can be read while it is half-written, and a truncated file must
    not crash the run that is merely trying to report on it. Falling back to the
    file's mtime keeps age-based reclamation working even when the contents are
    unusable.
    """
    fallback: datetime | None = None
    with contextlib.suppress(OSError):
        fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload.get("pid")
        started = payload.get("started_at")
        return LockInfo(
            pid=int(pid) if isinstance(pid, int | str) and str(pid).isdigit() else None,
            started_at=datetime.fromisoformat(started) if started else fallback,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return LockInfo(pid=None, started_at=fallback)


def _acquire(path: Path) -> bool:
    """Create the lock file exclusively. False means somebody else has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return True


@contextmanager
def pipeline_lock(path: Path, *, max_age_hours: int = 6) -> Iterator[LockInfo]:
    """Hold the pipeline lock for the duration of the block.

    Raises ``LockHeldError`` if another run owns it and the lock is younger than
    ``max_age_hours``.
    """
    if not _acquire(path):
        existing = read_lock(path)
        age = existing.age
        if age is None or age < timedelta(hours=max_age_hours):
            raise LockHeldError(
                f"another run holds {path} ({existing.describe()}). "
                "Wait for it to finish, or delete that file if you are certain "
                "no run is active."
            )
        logger.warning(
            "reclaiming lock %s abandoned by %s; older than %dh",
            path,
            existing.describe(),
            max_age_hours,
        )
        # A reclaim is two steps and another process can win between them, so a
        # failed re-acquire is reported rather than forced.
        path.unlink(missing_ok=True)
        if not _acquire(path):
            raise LockHeldError(f"another run took {path} while it was being reclaimed")

    try:
        yield read_lock(path)
    finally:
        path.unlink(missing_ok=True)
