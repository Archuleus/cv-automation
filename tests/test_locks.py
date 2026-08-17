from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from jobbot.locks import LockHeldError, pipeline_lock, read_lock


def write_lock(path, *, pid=4812, age_hours=0.0):
    started = datetime.now(UTC) - timedelta(hours=age_hours)
    path.write_text(
        json.dumps({"pid": pid, "started_at": started.isoformat()}), encoding="utf-8"
    )
    return path


class TestAcquireAndRelease:
    def test_creates_the_lock_file_while_held(self, tmp_path):
        path = tmp_path / "jobbot.lock"

        with pipeline_lock(path):
            assert path.exists()

        assert not path.exists()

    def test_records_the_holding_pid(self, tmp_path):
        path = tmp_path / "jobbot.lock"

        with pipeline_lock(path) as info:
            assert info.pid == os.getpid()

    def test_releases_the_lock_when_the_body_raises(self, tmp_path):
        # A crashed run that kept the lock would block every later run for hours.
        path = tmp_path / "jobbot.lock"

        with pytest.raises(RuntimeError, match="arm exploded"), pipeline_lock(path):
            raise RuntimeError("arm exploded")

        assert not path.exists()

    def test_creates_the_parent_directory(self, tmp_path):
        path = tmp_path / "var" / "jobbot.lock"

        with pipeline_lock(path):
            assert path.exists()


class TestContention:
    def test_refuses_when_a_fresh_lock_exists(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", pid=4812, age_hours=0.5)

        with pytest.raises(LockHeldError, match="4812"), pipeline_lock(path):
            pytest.fail("acquired a lock another run holds")

    def test_reports_how_long_the_other_run_has_been_going(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", age_hours=2.5)

        with pytest.raises(LockHeldError, match="2h30m"), pipeline_lock(path):
            pytest.fail("acquired a held lock")

    def test_a_held_lock_is_not_deleted_by_the_run_that_was_refused(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", age_hours=0.5)

        with pytest.raises(LockHeldError), pipeline_lock(path):
            pass

        assert path.exists()

    def test_nested_acquisition_of_the_same_path_is_refused(self, tmp_path):
        path = tmp_path / "jobbot.lock"

        with pipeline_lock(path), pytest.raises(LockHeldError), pipeline_lock(path):
            pytest.fail("acquired the lock twice")


class TestStaleness:
    def test_reclaims_a_lock_older_than_the_maximum_age(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", pid=999999, age_hours=9)

        with pipeline_lock(path, max_age_hours=6) as info:
            assert info.pid == os.getpid()

    def test_keeps_a_lock_that_is_exactly_within_the_maximum_age(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", age_hours=5.9)

        with pytest.raises(LockHeldError), pipeline_lock(path, max_age_hours=6):
            pytest.fail("reclaimed a lock that is still fresh")

    def test_a_corrupt_lock_falls_back_to_the_file_age(self, tmp_path):
        # A half-written lock file must not crash the next run, and must still be
        # reclaimable once it is old enough.
        path = tmp_path / "jobbot.lock"
        path.write_text('{"pid": 481', encoding="utf-8")
        old = (datetime.now(UTC) - timedelta(hours=9)).timestamp()
        os.utime(path, (old, old))

        with pipeline_lock(path, max_age_hours=6) as info:
            assert info.pid == os.getpid()

    def test_a_corrupt_fresh_lock_is_still_respected(self, tmp_path):
        path = tmp_path / "jobbot.lock"
        path.write_text("not json at all", encoding="utf-8")

        with pytest.raises(LockHeldError, match="pid unknown"), pipeline_lock(path):
            pytest.fail("acquired a lock held by an unidentifiable run")


class TestReadLock:
    def test_reports_a_missing_file_as_empty(self, tmp_path):
        info = read_lock(tmp_path / "absent.lock")

        assert info.pid is None
        assert info.started_at is None
        assert info.age is None

    def test_reads_pid_and_start_time(self, tmp_path):
        path = write_lock(tmp_path / "jobbot.lock", pid=321, age_hours=1)

        info = read_lock(path)

        assert info.pid == 321
        assert info.age is not None
        assert timedelta(minutes=55) < info.age < timedelta(minutes=65)

    def test_describes_an_unknown_start_time(self, tmp_path):
        path = tmp_path / "jobbot.lock"
        path.write_text(json.dumps({"pid": 7}), encoding="utf-8")
        # mtime always exists, so drop it by describing the parsed info directly.
        info = read_lock(path)

        assert info.pid == 7
        assert "pid 7" in info.describe()
