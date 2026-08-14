from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jobbot import db
from jobbot.cli import app
from jobbot.config import get_settings
from jobbot.connectors.ats import PROVIDERS
from jobbot.connectors.boards import load_boards

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path) -> Iterator[None]:
    """Point the CLI at throwaway files, never the developer's own data.

    The board registry is redirected too: `run --arm 1` performs real HTTP
    against every configured board, so a test that used the real registry would
    hammer several dozen third-party APIs.
    """
    empty_registry = tmp_path / "boards.json"
    empty_registry.write_text('{"boards": []}', encoding="utf-8")
    empty_seed = tmp_path / "tr_companies.json"
    empty_seed.write_text('{"companies": []}', encoding="utf-8")

    monkeypatch.setenv("JOBBOT_DATABASE_URL", f"sqlite:///{tmp_path / 'jobbot.db'}")
    monkeypatch.setenv("JOBBOT_BOARDS_PATH", str(empty_registry))
    monkeypatch.setenv("JOBBOT_TR_COMPANIES_PATH", str(empty_seed))
    monkeypatch.setenv("JOBBOT_APPLICANT_NAME", "Kerem")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    db.reset_engine()
    yield
    db.reset_engine()
    get_settings.cache_clear()


class TestVersion:
    def test_prints_version(self):
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "jobbot" in result.stdout


class TestDbCommands:
    def test_init_creates_schema(self, tmp_path):
        result = runner.invoke(app, ["db", "init"])

        assert result.exit_code == 0
        assert "[OK]" in result.stdout
        assert (tmp_path / "jobbot.db").exists()

    def test_init_is_idempotent(self):
        assert runner.invoke(app, ["db", "init"]).exit_code == 0
        assert runner.invoke(app, ["db", "init"]).exit_code == 0

    def test_status_lists_every_table(self):
        runner.invoke(app, ["db", "init"])

        result = runner.invoke(app, ["db", "status"])

        assert result.exit_code == 0
        for table in ("companies", "jobs", "contacts", "applications", "events"):
            assert table in result.stdout

    def test_reset_aborts_without_confirmation(self):
        runner.invoke(app, ["db", "init"])

        result = runner.invoke(app, ["db", "reset"], input="n\n")

        assert result.exit_code != 0

    def test_reset_proceeds_with_yes_flag(self):
        runner.invoke(app, ["db", "init"])

        result = runner.invoke(app, ["db", "reset", "--yes"])

        assert result.exit_code == 0
        assert "[OK]" in result.stdout


class TestConfigCheck:
    def test_reports_unconfigured_features_as_missing(self):
        result = runner.invoke(app, ["config", "check"])

        assert result.exit_code == 0
        assert "[MISSING]" in result.stdout
        assert "llm" in result.stdout

    def test_reports_configured_features_as_ready(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        get_settings.cache_clear()

        result = runner.invoke(app, ["config", "check"])

        assert result.exit_code == 0
        assert "[OK]" in result.stdout


class TestRun:
    def test_arm_1_refuses_to_run_with_an_empty_registry(self, profile):
        # Better a loud failure than a "successful" run that fetched nothing.
        # Depends on the profile fixture because arm 1 checks the profile first,
        # and without one it would refuse for that reason instead.
        result = runner.invoke(app, ["run", "--arm", "1"])

        assert result.exit_code == 2
        assert "no boards configured" in result.stdout

    def test_arm_2_refuses_to_run_with_an_empty_seed(self):
        # An implemented arm that would otherwise crawl 130 real company sites.
        result = runner.invoke(app, ["run", "--arm", "2"])

        assert result.exit_code == 2
        assert "no seed companies" in result.stdout

    def test_unimplemented_arm_exits_nonzero_rather_than_silently_succeeding(self):
        result = runner.invoke(app, ["run", "--arm", "3"])

        assert result.exit_code == 1
        assert "phase 4" in result.stdout

    def test_unknown_arm_is_a_usage_error(self):
        result = runner.invoke(app, ["run", "--arm", "9"])

        assert result.exit_code == 2
        assert "[FAIL]" in result.stdout

    def test_non_numeric_arm_is_a_usage_error(self):
        result = runner.invoke(app, ["run", "--arm", "discovery"])

        assert result.exit_code == 2


class TestBoards:
    def test_list_reads_the_configured_registry(self, monkeypatch, tmp_path):
        registry = tmp_path / "custom_boards.json"
        registry.write_text(
            '{"boards": [{"provider": "greenhouse", "token": "acme", "name": "Acme"}]}',
            encoding="utf-8",
        )
        monkeypatch.setenv("JOBBOT_BOARDS_PATH", str(registry))
        get_settings.cache_clear()

        result = runner.invoke(app, ["boards", "list"])

        assert result.exit_code == 0
        assert "acme" in result.stdout

    def test_add_rejects_an_unknown_provider(self):
        result = runner.invoke(app, ["boards", "add", "myspace", "acme"])

        assert result.exit_code == 2
        assert "unknown provider" in result.stdout

    @respx.mock
    def test_add_refuses_a_board_that_returns_nothing(self):
        # An unverified token polls nothing forever; adding one must be deliberate.
        respx.get(PROVIDERS["greenhouse"].endpoint("ghost")).mock(
            return_value=httpx.Response(404)
        )

        result = runner.invoke(app, ["boards", "add", "greenhouse", "ghost"])

        assert result.exit_code == 1
        assert "--force" in result.stdout

    @respx.mock
    def test_add_appends_a_live_board(self, tmp_path):
        respx.get(PROVIDERS["greenhouse"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "Dev"}]})
        )

        result = runner.invoke(
            app, ["boards", "add", "greenhouse", "acme", "--name", "Acme", "--country", "TR"]
        )

        assert result.exit_code == 0
        assert "added greenhouse:acme" in result.stdout
        assert any(board.token == "acme" for board in load_boards())

    @respx.mock
    def test_add_is_idempotent(self):
        respx.get(PROVIDERS["greenhouse"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "Dev"}]})
        )
        runner.invoke(app, ["boards", "add", "greenhouse", "acme"])

        result = runner.invoke(app, ["boards", "add", "greenhouse", "acme"])

        assert result.exit_code == 0
        assert "already in the registry" in result.stdout

    def test_jobs_top_reports_an_empty_database(self):
        runner.invoke(app, ["db", "init"])

        result = runner.invoke(app, ["jobs", "top"])

        assert result.exit_code == 0
        assert "no stored postings" in result.stdout
