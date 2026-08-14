"""The generate-check-retry loop, driven by a scripted model."""

from __future__ import annotations

from typing import Any

import pytest

from jobbot.llm.base import GenerationStats
from jobbot.llm.compose import CompositionFailedError, compose
from jobbot.profile import Profile
from tests.test_llm_validate import GOOD_BODY, POSTING


class ScriptedClient:
    """Returns a fixed sequence of payloads, recording what it was asked."""

    model = "stub"

    def __init__(self, *payloads: dict[str, str]) -> None:
        self._payloads = list(payloads)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def health(self) -> str:
        return "stub ready"

    def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], GenerationStats]:
        self.prompts.append(prompt)
        self.systems.append(system)
        payload = self._payloads.pop(0) if self._payloads else self._payloads_exhausted()
        return payload, GenerationStats(model=self.model, output_tokens=100)

    def _payloads_exhausted(self) -> dict[str, str]:
        raise AssertionError("client called more times than the script provides")


GOOD = {
    "subject": "Backend Engineer - Java, Spring Boot, PostgreSQL",
    "body": GOOD_BODY,
    "cited_detail": "cache-layer capabilities and analytics",
}
FABRICATED = {
    "subject": "Backend Engineer",
    "body": GOOD_BODY,
    "cited_detail": "your Rust compiler and Kubernetes operator team",
}
TOO_SHORT = {"subject": "Hi", "body": "I want the job.", "cited_detail": "structured SQL engines"}


def _compose(client: ScriptedClient, profile: Profile, **kwargs: Any):
    return compose(
        client,
        profile=profile,
        company_name="Canonical",
        title="Software Engineer - Data Infrastructure",
        location="Home based - EMEA",
        description=POSTING,
        language="en",
        **kwargs,
    )


class TestSuccess:
    def test_returns_a_valid_draft_on_the_first_attempt(self, profile: Profile):
        client = ScriptedClient(GOOD)

        result = _compose(client, profile)

        assert result.attempts == 1
        assert result.draft.subject.startswith("Backend Engineer")
        assert len(client.prompts) == 1

    def test_selects_the_cv_matching_the_language(self, profile: Profile):
        result = _compose(ScriptedClient(GOOD), profile)

        assert result.cv_variant == "backend_en"
        assert result.cv_file.endswith(".pdf")

    def test_prompt_carries_the_posting_and_the_profile(self, profile: Profile):
        client = ScriptedClient(GOOD)

        _compose(client, profile)

        prompt = client.prompts[0]
        assert "structured SQL engines" in prompt
        assert "spring boot" in prompt.lower()
        assert profile.name in prompt


class TestRetry:
    def test_retries_after_a_fabricated_citation_and_succeeds(self, profile: Profile):
        client = ScriptedClient(FABRICATED, GOOD)

        result = _compose(client, profile)

        assert result.attempts == 2

    def test_retry_prompt_names_the_specific_fault(self, profile: Profile):
        # A small model corrects a stated fault far more reliably than it
        # avoids one described in advance.
        client = ScriptedClient(FABRICATED, GOOD)

        _compose(client, profile)

        retry = client.prompts[1]
        assert "REJECTED" in retry
        assert "not in the posting" in retry

    def test_gives_up_after_the_attempt_limit(self, profile: Profile):
        client = ScriptedClient(FABRICATED, FABRICATED, FABRICATED)

        with pytest.raises(CompositionFailedError, match="after 3 attempts"):
            _compose(client, profile)

    def test_failure_never_returns_an_invalid_draft(self, profile: Profile):
        # The queue must not receive something that failed its own checks.
        client = ScriptedClient(TOO_SHORT, TOO_SHORT)

        with pytest.raises(CompositionFailedError):
            _compose(client, profile, max_attempts=2)

    def test_respects_a_lower_attempt_limit(self, profile: Profile):
        client = ScriptedClient(FABRICATED)

        with pytest.raises(CompositionFailedError, match="after 1 attempt"):
            _compose(client, profile, max_attempts=1)


class TestLanguage:
    def test_infers_turkish_from_the_posting(self, profile: Profile):
        turkish_posting = (
            "Ekibimize backend geliştirici arıyoruz. Java ve Spring Boot ile "
            "mikroservis mimarisi üzerinde çalışacak, PostgreSQL veritabanı ve "
            "Docker ile konteynerli dağıtım konularında deneyim sahibi olan "
            "adaylar tercih edilir. Ayrıca REST API tasarımı bilgisi gereklidir."
        )
        client = ScriptedClient(
            {
                "subject": "Backend Geliştirici - Java, Spring Boot, PostgreSQL",
                "body": (
                    "İlanınızda Java ve Spring Boot ile mikroservis mimarisi üzerinde "
                    "çalışacak bir geliştirici aradığınızı belirtmişsiniz. Bir muhasebe "
                    "projesinde Java 17 ve Spring Boot ile çift girişli defter "
                    "servisi geliştirdim, veri erişim katmanını Spring Data JPA ve "
                    "PostgreSQL ile kurdum, şema yönetimini Flyway ile yaptım ve "
                    "performans için Redis önbellek katmanı ekledim. Stajımda "
                    "Python ve FastAPI ile yazdığımız mikroservisi öğrenci bilgi "
                    "sistemine REST API üzerinden entegre ettim, böylece optik "
                    "formlar otomatik değerlendirilip sonuçlar sisteme geri aktı. "
                    "Bir cüzdan servisinde Spring Security ile durumsuz JWT "
                    "kimlik doğrulama, BCrypt parola özetleme ve rol tabanlı "
                    "yetkilendirme kurdum; para transferlerini atomik ve işlem "
                    "güvenli olacak şekilde tasarladım. Docker ile konteynerli "
                    "dağıtım ve JUnit ile test yazma konularında da pratik "
                    "deneyimim bulunuyor."
                ),
                "cited_detail": "Java ve Spring Boot ile mikroservis mimarisi",
            }
        )

        result = compose(
            client,
            profile=profile,
            company_name="Acme",
            title="Backend Geliştirici",
            location="İstanbul",
            description=turkish_posting,
        )

        assert result.draft.language == "tr"
        assert result.cv_variant == "backend_tr"
