"""The checks that decide whether a generated draft may reach the queue."""

from __future__ import annotations

import pytest

from jobbot.llm.base import Draft
from jobbot.llm.validate import (
    citation_is_grounded,
    unsupported_technologies,
    validate_draft,
)
from jobbot.profile import Profile

POSTING = (
    "Canonical is building a comprehensive automation suite to provide multi-cloud "
    "and on-premise data solutions for the enterprise. The data platform team "
    "develops a full range of data stores spanning big data, NoSQL, cache-layer "
    "capabilities and analytics, all the way to structured SQL engines. We have a "
    "number of openings ranging anywhere from junior to senior level."
)

GOOD_BODY = (
    "Your data platform team works across NoSQL, cache-layer capabilities and "
    "structured SQL engines, which lines up closely with what I have built. On a "
    "ledger project I designed a double-entry accounting service on Java and Spring "
    "Boot, managed the PostgreSQL schema with Flyway migrations, and added a Redis "
    "caching layer to keep read paths fast under load. During an internship I "
    "worked on a Python and FastAPI microservice that integrated with a student "
    "information system over a REST API, so I am comfortable with service "
    "boundaries and containerised deployment using Docker. I graduated in computer "
    "engineering this year and I am looking for a backend role where I can keep "
    "working on data-heavy systems in an open-source environment."
)


def _draft(**overrides: str) -> Draft:
    payload = {
        "subject": "Backend Engineer - Java, Spring Boot, PostgreSQL",
        "body": GOOD_BODY,
        "cited_detail": "cache-layer capabilities and analytics",
        "language": "en",
    }
    payload.update(overrides)
    return Draft(**payload)  # type: ignore[arg-type]


class TestCitationGrounding:
    def test_accepts_a_verbatim_quote(self):
        assert citation_is_grounded("structured SQL engines", POSTING)

    def test_accepts_a_quote_with_normalised_casing_and_spacing(self):
        assert citation_is_grounded("Structured  SQL   Engines", POSTING)

    def test_accepts_a_close_paraphrase(self):
        # Models drop articles and connectives while quoting honestly.
        assert citation_is_grounded("full range of data stores", POSTING)

    def test_rejects_an_invented_detail(self):
        # This is the failure the whole mechanism exists to catch.
        assert not citation_is_grounded("your Kubernetes operator for Kafka", POSTING)

    def test_rejects_a_phrase_too_short_to_be_a_detail(self):
        # A single word would match by accident and prove nothing.
        assert not citation_is_grounded("data", POSTING)

    def test_rejects_against_an_empty_posting(self):
        assert not citation_is_grounded("structured SQL engines", "")


class TestInventedCredentials:
    """The first real generation passed every other check and still lied."""

    # Verbatim from qwen3:8b's first draft against the real Canonical posting.
    REAL_FABRICATION = (
        "I have experience writing idiomatic Python code to create new features, as "
        "demonstrated by my work on a real-time data processing system using FastAPI "
        "and PostgreSQL. My background in distributed systems includes designing and "
        "implementing microservices with Spring Boot and Kafka, ensuring fault-tolerant "
        "replication and scalability. I hold a Bachelor's degree in Computer Engineering "
        "and have hands-on experience with Linux systems administration and Kubernetes "
        "clusters. I am also familiar with Redis and OpenSearch, which aligns with the "
        "technologies mentioned in the posting."
    )

    def test_catches_the_technologies_the_applicant_never_used(self, profile: Profile):
        invented = unsupported_technologies(self.REAL_FABRICATION, profile)

        assert set(invented) == {"kubernetes", "linux", "opensearch"}

    def test_the_draft_is_rejected_despite_a_genuine_citation(self, profile: Profile):
        # The citation was real; the career was not. A grounded quote proves the
        # model read the posting and says nothing about the rest of the letter.
        result = validate_draft(
            _draft(
                body=self.REAL_FABRICATION,
                cited_detail="idiomatic Python code to create new features",
            ),
            posting=POSTING + " Write high-quality, idiomatic Python code to create "
            "new features for our data platform.",
            profile=profile,
            expected_language="en",
        )

        assert not result.ok
        assert any("absent from the profile" in problem for problem in result.violations)

    def test_accepts_technologies_the_profile_does_list(self, profile: Profile):
        assert unsupported_technologies(GOOD_BODY, profile) == ()

    def test_a_project_only_technology_counts_as_supported(self, profile: Profile):
        # A tool named only in a project description still counts as supported;
        # the vocabulary is drawn from experience and projects, not just skills.
        vocabulary_only_in_a_project = next(
            (
                word
                for project in profile.projects
                for line in project.highlights
                for word in line.lower().split()
                if word.strip(".,") not in profile.skills.all
            ),
            None,
        )
        if vocabulary_only_in_a_project is None:
            pytest.skip("this profile lists no project-only vocabulary")

        assert unsupported_technologies(vocabulary_only_in_a_project, profile) == ()

    def test_ordinary_english_words_are_not_treated_as_claims(self, profile: Profile):
        # "go", "rust", "swift" and friends are technologies and also plain words.
        text = "I will go on to write swift, solid code and rust-free pipelines."

        assert unsupported_technologies(text, profile) == ()


class TestValidateDraft:
    def test_accepts_a_good_draft(self, profile: Profile):
        result = validate_draft(
            _draft(), posting=POSTING, profile=profile, expected_language="en"
        )

        assert result.ok, result.violations

    def test_rejects_a_fabricated_citation(self, profile: Profile):
        result = validate_draft(
            _draft(cited_detail="your Rust compiler team"),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert not result.ok
        assert any("not in the posting" in problem for problem in result.violations)

    def test_rejects_an_empty_body(self, profile: Profile):
        result = validate_draft(
            _draft(body="   "), posting=POSTING, profile=profile, expected_language="en"
        )

        assert result.violations == ("body is empty",)

    def test_rejects_a_body_that_is_too_short(self, profile: Profile):
        result = validate_draft(
            _draft(body="I would like to apply for this role."),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("minimum" in problem for problem in result.violations)

    def test_rejects_a_body_that_is_too_long(self, profile: Profile):
        result = validate_draft(
            _draft(body=GOOD_BODY + " " + " ".join(["padding"] * 200)),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("maximum" in problem for problem in result.violations)

    @pytest.mark.parametrize(
        "filler",
        ["I am writing to express my interest.", "You have a truly dynamic team."],
    )
    def test_rejects_filler_phrases(self, profile: Profile, filler: str):
        result = validate_draft(
            _draft(body=f"{filler} {GOOD_BODY}"),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("filler phrase" in problem for problem in result.violations)

    @pytest.mark.parametrize("marker", ["[Company Name]", "{{position}}", "TODO"])
    def test_rejects_unfilled_placeholders(self, profile: Profile, marker: str):
        result = validate_draft(
            _draft(body=f"{GOOD_BODY} I am applying to {marker}."),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("placeholder" in problem for problem in result.violations)

    def test_rejects_the_wrong_language(self, profile: Profile):
        turkish_body = (
            "Veri platformu ekibinizin NoSQL ve yapılandırılmış SQL motorları ile "
            "çalışması ilgimi çekti. Bir defter projesinde Java ve Spring Boot ile "
            "çift girişli muhasebe servisi geliştirdim, PostgreSQL şemasını Flyway "
            "ile yönettim ve okuma yollarını hızlandırmak için Redis önbellek katmanı "
            "ekledim. Stajımda Python ve FastAPI ile bir mikroservis üzerinde "
            "çalıştım ve bu servisi REST API üzerinden öğrenci bilgi sistemine "
            "entegre ettim. Docker ile konteynerli dağıtım konusunda da deneyimim var."
        )

        result = validate_draft(
            _draft(body=turkish_body),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("written in tr" in problem for problem in result.violations)

    def test_rejects_an_inflated_experience_claim(self, profile: Profile):
        # The profile says roughly one year; a model that writes "8 years" has
        # invented a career, which no reviewer should have to catch by reading.
        result = validate_draft(
            _draft(body=f"With 8 years of experience in backend systems. {GOOD_BODY}"),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert any("years of experience" in problem for problem in result.violations)

    def test_allows_a_modest_rounding_of_experience(self, profile: Profile):
        result = validate_draft(
            _draft(body=f"Over 2 years of hands-on backend work. {GOOD_BODY}"),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert not any("years of experience" in problem for problem in result.violations)

    def test_rejects_an_overlong_subject(self, profile: Profile):
        result = validate_draft(
            _draft(subject="x" * 130), posting=POSTING, profile=profile, expected_language="en"
        )

        assert any("subject is" in problem for problem in result.violations)

    def test_reports_every_violation_at_once(self, profile: Profile):
        result = validate_draft(
            _draft(body="Too short.", cited_detail="invented thing entirely"),
            posting=POSTING,
            profile=profile,
            expected_language="en",
        )

        assert len(result.violations) >= 2
