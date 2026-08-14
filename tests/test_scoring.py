from __future__ import annotations

import pytest

from jobbot.normalize import Seniority
from jobbot.profile import Profile
from jobbot.scoring import Posting, score_posting


def _score(profile: Profile, **kwargs: str):
    return score_posting(Posting(**kwargs), profile)  # type: ignore[arg-type]


class TestHardRejections:
    def test_rejects_senior_roles(self, profile: Profile):
        result = _score(profile, title="Senior Java Developer", location="Istanbul")

        assert result.rejected
        assert "above target" in result.reject_reason
        assert result.total == 0

    def test_rejects_management_roles(self, profile: Profile):
        assert _score(profile, title="Engineering Manager", location="Istanbul").rejected

    @pytest.mark.parametrize(
        "title",
        ["Yazılım Mühendisliği Stajyeri", "Backend Developer Intern", "Yazılım Stajyeri"],
    )
    def test_rejects_internships(self, profile: Profile, title: str):
        # The applicant has graduated; internships are below target.
        result = _score(
            profile,
            title=title,
            location="Istanbul, Türkiye",
            description="Java, Spring Boot, PostgreSQL, Docker",
        )

        assert result.rejected
        assert "intern" in result.reject_reason

    def test_rejects_excluded_domains(self, profile: Profile):
        result = _score(profile, title="SAP ABAP Developer", location="Istanbul")

        assert result.rejected
        assert "excluded keyword" in result.reject_reason

    def test_excluded_keyword_in_the_description_does_not_reject(self, profile: Profile):
        # "you will work closely with our sales team" is not a sales job.
        result = _score(
            profile,
            title="Backend Developer",
            location="Istanbul, Türkiye",
            description=(
                "Java, Spring Boot, PostgreSQL, Docker. You will work closely with "
                "our sales team and support their Salesforce integration."
            ),
        )

        assert not result.rejected

    def test_rejects_onsite_jobs_abroad(self, profile: Profile):
        # No visa sponsorship means an onsite role abroad is unreachable.
        result = _score(
            profile,
            title="Backend Developer",
            location="Berlin, Germany",
            description="Java Spring Boot PostgreSQL Docker onsite in our Berlin office",
        )

        assert result.rejected
        assert "relocation not available" in result.reject_reason

    def test_rejects_region_locked_remote(self, profile: Profile):
        result = _score(
            profile,
            title="Backend Developer",
            location="Remote (US only)",
            description="Java Spring Boot PostgreSQL Docker REST API",
        )

        assert result.rejected
        assert "restricted to" in result.reject_reason

class TestLiveRunRegressions:
    """Cases that a real 8,268-posting run got wrong."""

    @pytest.mark.parametrize(
        "title",
        ["Staff Software Engineer", "Staff Security Software Engineer", "Principal Engineer"],
    )
    def test_rejects_staff_titles(self, profile: Profile, title: str):
        # "staff engineer" as a phrase never appears; real titles put a word
        # between them. These scored 58-68 and were stored.
        result = _score(
            profile,
            title=title,
            location="Istanbul, Türkiye",
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert result.rejected

    def test_remote_wording_in_a_description_does_not_make_an_onsite_job_remote(
        self, profile: Profile
    ):
        # "we are a remote-friendly company" on a San Francisco posting.
        result = _score(
            profile,
            title="Backend Engineer",
            location="San Francisco, CA",
            description=(
                "Java, Spring Boot, PostgreSQL. We are a remote-friendly company "
                "with a distributed culture and offer work from home flexibility."
            ),
        )

        assert result.rejected
        assert "US" in result.reject_reason

    @pytest.mark.parametrize(
        "location",
        ["Remote - Ontario, Canada", "United States - Remote", "Remote - Estonia", "Remote, India"],
    )
    def test_rejects_remote_fenced_to_a_named_country(self, profile: Profile, location: str):
        result = _score(
            profile,
            title="Backend Developer",
            location=location,
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert result.rejected

    @pytest.mark.parametrize("location", ["Budapest, Hungary", "Athens, Greece", "São Paulo"])
    def test_rejects_onsite_in_countries_outside_the_original_map(
        self, profile: Profile, location: str
    ):
        result = _score(
            profile,
            title="Backend Developer",
            location=location,
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert result.rejected

    def test_rejects_onsite_at_an_unrecognised_location(self, profile: Profile):
        # An unknown place name is not evidence of being local.
        result = _score(
            profile,
            title="Backend Developer",
            location="Someplace, Freedonia",
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert result.rejected
        assert "unrecognised location" in result.reject_reason

    @pytest.mark.parametrize(
        "location", ["Remote-EMEA", "Remote (Worldwide)", "Anywhere", "Remote - Europe"]
    )
    def test_accepts_remote_regions_that_include_turkiye(self, profile: Profile, location: str):
        result = _score(
            profile,
            title="Backend Developer",
            location=location,
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert not result.rejected

    @pytest.mark.parametrize(
        "location", ["Remote - EU", "Remote-NORAM", "Remote-Western Europe", "Remote-UK&I"]
    )
    def test_rejects_remote_regions_that_exclude_turkiye(self, profile: Profile, location: str):
        result = _score(
            profile,
            title="Backend Developer",
            location=location,
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert result.rejected

    def test_member_of_technical_staff_is_not_rejected_as_senior(self, profile: Profile):
        result = _score(
            profile,
            title="Member of Technical Staff, Backend",
            location="Remote-EMEA",
            description="Python, FastAPI, PostgreSQL, Docker",
        )

        assert not result.rejected

    def test_still_accepts_unfenced_remote(self, profile: Profile):
        result = _score(
            profile,
            title="Backend Developer",
            location="Remote",
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert not result.rejected


class TestHardRejectionsContinued:
    def test_rejects_unrelated_roles(self, profile: Profile):
        result = _score(
            profile,
            title="Marketing Coordinator",
            location="Istanbul",
            description="Manage campaigns and social media presence",
        )

        assert result.rejected
        assert result.reject_reason == "no role or skill overlap"


class TestAcceptedPostings:
    def test_ideal_turkish_junior_backend_role_scores_high(self, profile: Profile):
        result = _score(
            profile,
            title="Junior Backend Developer",
            location="Istanbul, Türkiye",
            description=(
                "Java, Spring Boot, Spring Data JPA, PostgreSQL, Docker, REST API, "
                "JWT, microservices, Redis, Git, JUnit"
            ),
        )

        assert not result.rejected
        assert result.total >= 85
        assert result.detected_seniority is Seniority.JUNIOR
        assert result.detected_country == "TR"

    def test_turkish_posting_in_turkish_language_scores_high(self, profile: Profile):
        result = _score(
            profile,
            title="Yeni Mezun Yazılım Geliştirici",
            location="Bursa, Türkiye",
            description="Java, Spring Boot, PostgreSQL, Docker, REST API bilgisi",
        )

        assert not result.rejected
        assert result.total >= 75

    def test_unfenced_remote_abroad_is_accepted_but_ranked_lower(self, profile: Profile):
        turkish = _score(
            profile,
            title="Junior Backend Developer",
            location="Istanbul, Türkiye",
            description="Java Spring Boot PostgreSQL Docker REST API microservices",
        )
        abroad = _score(
            profile,
            title="Junior Backend Developer",
            location="Remote",
            description="Java Spring Boot PostgreSQL Docker REST API microservices",
        )

        # Both viable; the 70/30 split must show up as a ranking gap.
        assert not abroad.rejected
        assert abroad.total < turkish.total

    def test_secondary_stack_scores_lower_than_core_stack(self, profile: Profile):
        core = _score(
            profile,
            title="Backend Developer",
            location="Istanbul",
            description="Java Spring Boot Spring Security PostgreSQL Docker microservices",
        )
        secondary = _score(
            profile,
            title="Mobile Developer",
            location="Istanbul",
            description="React Native and Flutter development with Firebase",
        )

        assert core.total > secondary.total

    @pytest.mark.parametrize(
        "title",
        [
            "Yazılım Mühendisi",
            "Yazılım Mühendisliği",
            "Yazılım Geliştiricisi",
            "Backend Geliştirici",
            "Mobil Yazılım Geliştiricisi",
        ],
    )
    def test_turkish_suffixes_still_match_a_role_family(self, profile: Profile, title: str):
        # Turkish is agglutinative: "mühendis" becomes "mühendisi",
        # "mühendisliği". Without suffix matching these postings score 0 on role
        # and drop below the threshold despite being exactly on target.
        result = _score(
            profile,
            title=title,
            location="Istanbul, Türkiye",
            description="Java, Spring Boot, PostgreSQL, Docker",
        )

        assert not result.rejected
        assert result.role > 0, f"{title!r} matched no role family"

    def test_unknown_seniority_is_not_rejected(self, profile: Profile):
        result = _score(
            profile,
            title="Backend Developer",
            location="Istanbul",
            description="Java Spring Boot PostgreSQL",
        )

        assert not result.rejected
        assert result.detected_seniority is Seniority.UNKNOWN


class TestExplanation:
    def test_accepted_posting_explains_its_score(self, profile: Profile):
        result = _score(
            profile,
            title="Junior Backend Developer",
            location="Istanbul, Türkiye",
            description="Java Spring Boot PostgreSQL Docker",
        )

        assert "role family 'backend'" in result.explanation
        assert "core skills" in result.explanation
        assert "in TR" in result.explanation

    def test_rejected_posting_explains_the_rejection(self, profile: Profile):
        result = _score(profile, title="Senior Java Developer", location="Istanbul")

        assert result.explanation.startswith("rejected:")

    def test_components_sum_to_total(self, profile: Profile):
        result = _score(
            profile,
            title="Junior Backend Developer",
            location="Istanbul",
            description="Java Spring Boot PostgreSQL Docker REST API",
        )

        parts = result.role + result.skills + result.seniority + result.geography
        assert result.total == min(100, parts)
