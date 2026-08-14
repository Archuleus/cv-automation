from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jobbot.config import MissingConfigError, Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestValidation:
    def test_rejects_unknown_log_level(self):
        with pytest.raises(ValidationError):
            _settings(log_level="LOUD")

    def test_normalises_log_level_case(self):
        assert _settings(log_level="debug").log_level == "DEBUG"

    def test_refuses_to_disable_robots_compliance(self):
        # Ignoring robots.txt is never a valid configuration for this project.
        with pytest.raises(ValidationError):
            _settings(respect_robots=False)

    def test_rejects_daily_cap_above_hard_limit(self):
        with pytest.raises(ValidationError):
            _settings(max_applications_per_day=500)

    def test_rejects_zero_daily_cap(self):
        with pytest.raises(ValidationError):
            _settings(max_applications_per_day=0)


class TestFeatureRequirements:
    def test_llm_is_configured_by_default(self):
        # The model runs locally, so there is no key to supply. Whether it is
        # actually pulled and reachable is a runtime check, not a config one.
        assert _settings().has("llm")

    def test_llm_is_unconfigured_without_a_host(self):
        settings = _settings(llm_host="")

        assert not settings.has("llm")
        assert settings.missing_for("llm") == ["llm_host"]

    def test_require_raises_with_actionable_env_names(self):
        settings = _settings(smtp_password="")

        with pytest.raises(MissingConfigError) as excinfo:
            settings.require("email")

        # The message must name the environment variable, not the field.
        assert "JOBBOT_SMTP_PASSWORD" in str(excinfo.value)

    def test_require_is_silent_when_satisfied(self):
        _settings().require("llm")

    def test_email_reports_every_missing_setting(self):
        # smtp_host has a default, so it is not reported as missing.
        assert _settings().missing_for("email") == [
            "smtp_username",
            "smtp_password",
            "applicant_email",
        ]

    def test_email_requirements_follow_the_chosen_provider(self):
        # Selecting Outlook must not keep demanding SMTP settings.
        outlook = _settings(mail_provider="outlook")

        assert outlook.missing_for("email") == ["ms_client_id", "applicant_email"]

    def test_unknown_feature_is_a_programming_error(self):
        with pytest.raises(ValueError):
            _settings().missing_for("telepathy")


class TestDbPath:
    def test_relative_sqlite_path_resolves_under_project_root(self):
        path = _settings(database_url="sqlite:///var/jobbot.db").db_path
        assert path is not None
        assert path.is_absolute()
        assert path.name == "jobbot.db"

    def test_absolute_sqlite_path_is_preserved(self):
        absolute = Path("C:/tmp/x.db") if Path("C:/").exists() else Path("/tmp/x.db")
        path = _settings(database_url=f"sqlite:///{absolute}").db_path
        assert path == absolute

    def test_non_sqlite_url_has_no_filesystem_path(self):
        assert _settings(database_url="postgresql://u@h/db").db_path is None
