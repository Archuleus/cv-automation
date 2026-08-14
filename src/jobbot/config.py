"""Application configuration.

All settings come from the environment (or a local .env file). Nothing here is
hardcoded, and required secrets are validated at startup rather than at the
moment of first use, so a misconfigured run fails immediately instead of
halfway through a batch.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Sending outreach requires credentials that do not exist in phases 0-3. Rather
# than scatter `if key is None` checks, each feature declares the settings it
# needs and `Settings.require()` validates them at entry points.
FEATURE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    # The LLM runs locally, so "configured" only means a host and model are
    # named. Whether the model is actually pulled and reachable is a runtime
    # question -- see jobbot.llm.ollama.OllamaClient.health.
    "llm": ("llm_host", "llm_model"),
    "adzuna": ("adzuna_app_id", "adzuna_app_key"),
}

# "email" resolves through mail_provider rather than being a fixed list, so
# `config check` reports what the *selected* transport is missing instead of
# demanding settings for one that is not in use.
MAIL_PROVIDER_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "smtp": ("smtp_host", "smtp_username", "smtp_password", "applicant_email"),
    "outlook": ("ms_client_id", "applicant_email"),
}


class Env(StrEnum):
    DEV = "dev"
    PROD = "prod"
    TEST = "test"


class MissingConfigError(RuntimeError):
    """Raised when a feature is used without the settings it requires."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBBOT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Env = Env.DEV
    database_url: str = "sqlite:///var/jobbot.db"
    log_level: str = "INFO"
    # Registry of ATS board tokens arm 1 polls. Configurable so tests and
    # experiments can run against a different set without touching the real one.
    boards_path: str = "data/boards.json"
    # Seed employers arm 2 visits. Configurable for the same reason as
    # boards_path: a test must never be able to crawl 130 real company sites.
    tr_companies_path: str = "data/tr_companies.json"
    # Public member directories that company discovery harvests.
    directories_path: str = "data/directories.json"
    # Raises GitHub's search rate limit from ten requests a minute.
    github_token: str = Field(default="", alias="GITHUB_TOKEN")

    # Applicant identity
    applicant_name: str = ""
    applicant_email: str = ""
    applicant_phone: str = ""
    applicant_linkedin: str = ""
    applicant_github: str = ""

    # Safety caps. These are the guardrails that keep the system from turning
    # into a spam cannon; see docs/safety.md for the reasoning behind each.
    max_applications_per_day: int = Field(default=20, ge=1, le=100)
    company_cooldown_days: int = Field(default=30, ge=1)
    domain_rate_limit: float = Field(default=0.5, gt=0, le=10)
    respect_robots: bool = True

    # Matching
    min_match_score: int = Field(default=55, ge=0, le=100)
    review_confidence_threshold: int = Field(default=70, ge=0, le=100)

    # LLM. Used by arm 3 only -- scoring is rule-based and needs no model.
    # Runs locally through Ollama, so there is no API key and no per-request cost.
    llm_host: str = "http://localhost:11434"
    llm_model: str = "qwen3:8b"
    # Generation is slow on a 4 GB GPU and nothing here is latency-sensitive:
    # the output lands in a queue a human reads later.
    llm_timeout_seconds: int = Field(default=600, ge=30)
    # Context has to hold the posting (~1.2k tokens) plus profile and
    # instructions. Larger windows cost VRAM, which is the scarce resource.
    llm_context_tokens: int = Field(default=8192, ge=2048)

    # Email. Applications are sent through Microsoft Graph with the Mail.Send
    # scope and nothing else: the token can send, and cannot read the mailbox,
    # list contacts or delete anything. No password is ever stored, and access
    # is revocable from the Microsoft account page at any time.
    # Which transport actually sends. "smtp" uses an app password; "outlook"
    # uses a Mail.Send OAuth token, which is narrower but needs an Entra app
    # registration that personal Microsoft accounts frequently cannot create.
    mail_provider: Literal["smtp", "outlook"] = "smtp"

    # SMTP. The app password is a revocable, single-purpose credential -- not
    # the account password -- but it is broader than the OAuth scope: it grants
    # POP/IMAP/SMTP, so it can read the mailbox as well as send from it.
    smtp_host: str = "smtp-mail.outlook.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: int = Field(default=60, ge=5)
    # Where replies should land. Providers force the From header to match the
    # authenticated account, so this is the only way to route answers to a
    # different mailbox -- for example the address printed on the CV.
    reply_to_email: str = ""

    ms_client_id: str = ""
    ms_token_path: str = "var/ms_token.json"
    # Sending is paced. A burst of identical-looking mail from a new sender is
    # what gets an address filtered, and the damage is not recoverable.
    send_min_gap_seconds: int = Field(default=90, ge=10)
    send_max_gap_seconds: int = Field(default=420, ge=10)

    # Optional third-party APIs
    adzuna_app_id: str = Field(default="", alias="ADZUNA_APP_ID")
    adzuna_app_key: str = Field(default="", alias="ADZUNA_APP_KEY")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalised = value.upper()
        if normalised not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("respect_robots")
    @classmethod
    def _robots_must_stay_on(cls, value: bool) -> bool:
        # Disabling this is never a legitimate configuration for this project.
        if not value:
            raise ValueError("respect_robots cannot be disabled")
        return value

    @property
    def db_path(self) -> Path | None:
        """Filesystem path for SQLite URLs, or None for server-backed databases."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix) :]
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def boards_file(self) -> Path:
        path = Path(self.boards_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def directories_file(self) -> Path:
        path = Path(self.directories_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def tr_companies_file(self) -> Path:
        path = Path(self.tr_companies_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def ms_token_file(self) -> Path:
        path = Path(self.ms_token_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @model_validator(mode="after")
    def _send_gap_is_a_range(self) -> Settings:
        if self.send_max_gap_seconds < self.send_min_gap_seconds:
            raise ValueError(
                "send_max_gap_seconds must be >= send_min_gap_seconds, got "
                f"{self.send_max_gap_seconds} < {self.send_min_gap_seconds}"
            )
        return self

    def required_for(self, feature: str) -> tuple[str, ...]:
        """The settings a feature needs, resolved for the current configuration."""
        if feature == "email":
            return MAIL_PROVIDER_REQUIREMENTS[self.mail_provider]
        try:
            return FEATURE_REQUIREMENTS[feature]
        except KeyError:
            raise ValueError(f"unknown feature {feature!r}") from None

    def missing_for(self, feature: str) -> list[str]:
        """Return the names of settings a feature needs but does not have."""
        return [name for name in self.required_for(feature) if not getattr(self, name)]

    def has(self, feature: str) -> bool:
        return not self.missing_for(feature)

    def require(self, feature: str) -> None:
        """Fail loudly if `feature` is unusable with the current configuration."""
        missing = self.missing_for(feature)
        if missing:
            env_names = ", ".join(_env_var_name(name) for name in missing)
            raise MissingConfigError(
                f"feature {feature!r} requires the following unset settings: {env_names}. "
                "Add them to your .env (see .env.example)."
            )


def _env_var_name(field_name: str) -> str:
    """Map a settings field back to the environment variable that fills it."""
    field = Settings.model_fields[field_name]
    if field.alias:
        return field.alias
    return f"JOBBOT_{field_name.upper()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
