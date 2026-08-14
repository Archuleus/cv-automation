from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jobbot.config import PROJECT_ROOT
from jobbot.profile import Profile, ProfileNotFoundError, load_profile

MINIMAL = {
    "name": "Test Person",
    "skills": {"core": ["Java", "Spring Boot"]},
    "role_families": [{"name": "backend", "titles": ["Backend Developer"]}],
}


def _write(tmp_path, payload) -> object:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoad:
    def test_loads_the_real_profile(self, profile: Profile):
        assert profile.name
        assert profile.skills.core
        assert profile.role_families

    def test_the_shipped_example_is_valid(self):
        # The example is what a new user copies; if it does not parse, their
        # first command fails with a validation error instead of working.
        example = load_profile(PROJECT_ROOT / "cv" / "profile.example.json")

        assert example.skills.core
        assert example.role_families

    def test_missing_profile_explains_how_to_create_one(self, tmp_path):
        with pytest.raises(ProfileNotFoundError, match=r"profile\.json"):
            load_profile(tmp_path / "absent.json")

    def test_unknown_fields_are_rejected(self, tmp_path):
        # A typo in the profile would otherwise skew every score silently.
        path = _write(tmp_path, {**MINIMAL, "seniorty_targets": ["junior"]})

        with pytest.raises(ValidationError):
            load_profile(path)

    def test_underscore_keys_are_treated_as_comments(self, tmp_path):
        # JSON has no comment syntax and this file is filled in by hand.
        path = _write(tmp_path, {**MINIMAL, "_note": "explain a choice here"})

        assert load_profile(path).name == "Test Person"

    def test_skills_are_lowercased(self, tmp_path):
        profile = load_profile(_write(tmp_path, MINIMAL))

        assert profile.skills.core == ("java", "spring boot")

    def test_all_skills_is_the_union_of_the_tiers(self, tmp_path):
        payload = {
            **MINIMAL,
            "skills": {"core": ["Java"], "secondary": ["Dart"], "exploratory": ["RAG"]},
        }

        profile = load_profile(_write(tmp_path, payload))

        assert profile.skills.all == frozenset({"java", "dart", "rag"})


class TestValidation:
    def test_seniority_cannot_be_both_target_and_excluded(self, tmp_path):
        payload = {
            **MINIMAL,
            "seniority_targets": ["junior", "senior"],
            "seniority_excluded": ["senior"],
        }

        with pytest.raises(ValidationError, match="both targets and excluded"):
            load_profile(_write(tmp_path, payload))

    def test_geography_weights_must_sum_to_one(self, tmp_path):
        payload = {
            **MINIMAL,
            "geography": {"primary_weight": 0.7, "remote_abroad_weight": 0.5},
        }

        with pytest.raises(ValidationError, match=r"must equal 1\.0"):
            load_profile(_write(tmp_path, payload))

    def test_valid_weights_are_accepted(self, tmp_path):
        payload = {
            **MINIMAL,
            "geography": {"primary_weight": 0.6, "remote_abroad_weight": 0.4},
        }

        assert load_profile(_write(tmp_path, payload)).geography.primary_weight == 0.6


class TestLinks:
    def test_real_profile_leads_with_the_portfolio(self, profile: Profile):
        labels = [label for label, _ in profile.links]

        assert labels[0] == "Portfolio"
        assert dict(profile.links)["Portfolio"].startswith("https://")

    def test_empty_fields_are_dropped(self, tmp_path):
        profile = load_profile(_write(tmp_path, {**MINIMAL, "github": "https://gh/x"}))

        assert profile.links == (("GitHub", "https://gh/x"),)

    def test_no_links_when_nothing_is_configured(self, tmp_path):
        assert load_profile(_write(tmp_path, MINIMAL)).links == ()


class TestCvSelection:
    def test_picks_the_variant_matching_the_language(self, profile: Profile):
        chosen = profile.cv_for("tr")

        assert chosen is not None
        assert chosen[1].language == "tr"

    def test_falls_back_to_any_variant(self, tmp_path):
        payload = {
            **MINIMAL,
            "cv_variants": {"en_only": {"file": "cv/x.pdf", "language": "en"}},
        }

        chosen = load_profile(_write(tmp_path, payload)).cv_for("tr")

        assert chosen is not None
        assert chosen[1].language == "en"

    def test_no_variants_configured(self, tmp_path):
        assert load_profile(_write(tmp_path, MINIMAL)).cv_for("en") is None
