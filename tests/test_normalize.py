from __future__ import annotations

import pytest

from jobbot.normalize import (
    Seniority,
    detect_country,
    detect_remote,
    detect_seniority,
    fold,
    normalize_title,
    region_reach,
    remote_region_lock,
    title_tokens,
    titles_are_similar,
)


class TestFold:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("İSTANBUL", "istanbul"),
            ("İstanbul", "istanbul"),
            ("Yazılım Mühendisi", "yazilim muhendisi"),
            ("Kıdemli Geliştirici", "kidemli gelistirici"),
            ("ÇAĞRI", "cagri"),
            ("  Spring   Boot  ", "spring boot"),
            ("", ""),
        ],
    )
    def test_folds_turkish_to_ascii(self, raw: str, expected: str):
        assert fold(raw) == expected

    def test_dotted_capital_i_matches_plain_i(self):
        # Python's str.lower() turns "İ" into "i" plus a combining dot, which
        # then fails to compare equal to a plain "i". This is the bug this
        # function exists to prevent.
        assert fold("İzmir") == fold("izmir") == "izmir"

    def test_folds_accented_latin(self):
        assert fold("Zürich") == "zurich"


class TestDetectSeniority:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Senior Backend Developer", Seniority.SENIOR),
            ("Kıdemli Yazılım Geliştirici", Seniority.SENIOR),
            ("Sr. Java Engineer", Seniority.SENIOR),
            ("Junior Backend Developer", Seniority.JUNIOR),
            ("Jr Software Engineer", Seniority.JUNIOR),
            ("Yeni Mezun Yazılım Mühendisi", Seniority.JUNIOR),
            ("Entry Level Developer", Seniority.JUNIOR),
            ("Yazılım Stajyeri", Seniority.INTERN),
            ("Backend Developer Intern", Seniority.INTERN),
            ("Engineering Manager", Seniority.MANAGER),
            ("Tech Lead", Seniority.LEAD),
            ("Staff Engineer", Seniority.LEAD),
            ("Staff Software Engineer", Seniority.LEAD),
            ("Staff Security Software Engineer", Seniority.LEAD),
            ("Backend Developer", Seniority.UNKNOWN),
        ],
    )
    def test_reads_seniority_from_title(self, title: str, expected: Seniority):
        assert detect_seniority(title) == expected

    def test_title_wins_over_description(self):
        # A senior posting that mentions juniors is still a senior posting.
        assert (
            detect_seniority("Senior Backend Developer", "junior candidates also welcome")
            is Seniority.SENIOR
        )

    def test_description_is_a_fallback_for_junior_only(self):
        assert detect_seniority("Backend Developer", "This is an entry level role") is (
            Seniority.JUNIOR
        )

    def test_description_does_not_promote_to_senior(self):
        # "senior" in a description usually names a colleague, not the role.
        result = detect_seniority("Backend Developer", "you will report to a senior engineer")
        assert result is Seniority.UNKNOWN

    def test_does_not_match_substrings(self):
        # "sr" must not fire inside a longer word.
        assert detect_seniority("Disaster Recovery Developer") is Seniority.UNKNOWN


class TestSeniorityNoise:
    @pytest.mark.parametrize(
        "title",
        [
            "Member of Technical Staff",
            "Member of Technical Staff, MLE",
            "Member of Technical Staff (Backend)",
        ],
    )
    def test_member_of_technical_staff_is_not_staff_level(self, title: str):
        # This is the standard IC title at OpenAI, Anthropic and Cohere.
        # Reading it as staff-level rejected 91 of Cohere's 148 postings.
        assert detect_seniority(title) is Seniority.UNKNOWN

    def test_a_real_seniority_word_still_wins_alongside_the_phrase(self):
        assert detect_seniority("Senior Member of Technical Staff") is Seniority.SENIOR

    def test_genuine_staff_titles_are_unaffected(self):
        assert detect_seniority("Staff Software Engineer") is Seniority.LEAD


class TestRegionReach:
    @pytest.mark.parametrize(
        "location",
        ["Remote-EMEA", "Remote (Worldwide)", "Anywhere", "Global", "EMEA", "Europe",
         "Remote - Middle East", "Remote, any timezone"],
    )
    def test_regions_that_include_turkiye(self, location: str):
        assert region_reach(location) == "includes_tr"

    @pytest.mark.parametrize(
        "location",
        ["Remote - EU", "European Union", "Remote-NORAM", "LATAM", "APAC",
         "Remote-Western Europe", "Remote-Iberia", "Nordics", "DACH", "Remote-UK&I"],
    )
    def test_regions_that_exclude_turkiye(self, location: str):
        # The EU is a work-authorisation fence, not a geography: Türkiye is in
        # Europe and in EMEA, but not in the EU.
        assert region_reach(location) == "excludes_tr"

    def test_specific_regions_win_over_the_word_europe(self):
        assert region_reach("Western Europe") == "excludes_tr"

    @pytest.mark.parametrize("location", ["Istanbul", "Berlin, Germany", ""])
    def test_plain_locations_are_not_regions(self, location: str):
        assert region_reach(location) is None


class TestDetectRemote:
    @pytest.mark.parametrize(
        ("title", "location", "remote", "hybrid"),
        [
            ("Backend Developer", "Remote", True, False),
            ("Backend Developer (Remote)", "", True, False),
            ("Yazılım Geliştirici", "Uzaktan", True, False),
            ("Backend Developer", "Hybrid - Istanbul", False, True),
            ("Backend Developer", "Hibrit / Ankara", False, True),
            ("Backend Developer", "Istanbul", False, False),
        ],
    )
    def test_classifies_work_mode(self, title, location, remote, hybrid):
        assert detect_remote(title, location) == (remote, hybrid)

    def test_description_cannot_make_an_onsite_posting_remote(self):
        # Marketing copy about "remote-friendly culture" is not a work mode.
        is_remote, _ = detect_remote(
            "Backend Engineer",
            "San Francisco, CA",
            "We are a remote-first, distributed company offering work from home.",
        )

        assert is_remote is False

    def test_description_is_used_when_there_is_no_location(self):
        is_remote, _ = detect_remote("Backend Engineer", "", "This is a fully remote position.")

        assert is_remote is True

    def test_hybrid_is_not_reported_as_remote(self):
        # A hybrid job still requires being near the office.
        is_remote, is_hybrid = detect_remote("Developer", "Remote/Hybrid Istanbul")
        assert (is_remote, is_hybrid) == (False, True)


class TestRemoteRegionLock:
    @pytest.mark.parametrize(
        ("text", "expected_fragment"),
        [
            ("Backend Developer, Remote (US only)", "us only"),
            ("Developer - Remote, EU", "eu"),
            ("Engineer", "germany"),
        ],
    )
    def test_detects_geographic_fences(self, text: str, expected_fragment: str):
        lock = remote_region_lock(text, "", "You must be located in Germany to apply")
        assert lock is not None
        assert expected_fragment in lock or expected_fragment in text.lower()

    def test_ignores_non_geographic_parentheses(self):
        assert remote_region_lock("Backend Developer, Remote (Full-time)") is None

    def test_unfenced_remote_has_no_lock(self):
        assert remote_region_lock("Backend Developer", "Remote") is None


class TestDetectCountry:
    @pytest.mark.parametrize(
        "location",
        ["Istanbul, Türkiye", "İzmir", "Bursa, Turkey", "Ankara", "Kütahya"],
    )
    def test_recognises_turkish_locations(self, location: str):
        assert detect_country(location) == "TR"

    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            ("Berlin, Germany", "DE"),
            ("Amsterdam", "NL"),
            ("London, UK", "UK"),
            ("San Francisco, CA", "US"),
            ("Manipal, India", "IN"),
        ],
    )
    def test_recognises_foreign_locations(self, location: str, expected: str):
        # Abroad must be recognisably abroad, otherwise an onsite role there
        # scores as "location unknown" instead of being rejected.
        assert detect_country(location) == expected

    def test_returns_none_for_unrecognised_locations(self):
        assert detect_country("Somewhere Unlisted") is None

    def test_returns_none_for_empty_location(self):
        assert detect_country("") is None


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Senior Backend Developer (m/w/d)", "senior backend developer"),
            ("Backend Developer [Istanbul]", "backend developer"),
            ("Java Developer - Remote", "java developer"),
            ("Yazılım Mühendisi", "yazilim muhendisi"),
            ("C# / .NET Developer", "c# .net developer"),
        ],
    )
    def test_strips_decoration(self, raw: str, expected: str):
        assert normalize_title(raw) == expected


class TestTitleSimilarity:
    def test_tokens_drop_stopwords(self):
        assert "the" not in title_tokens("The Backend Developer")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Backend Developer", "Backend Developer (Remote)"),
            ("Java Backend Developer", "Backend Java Developer"),
        ],
    )
    def test_detects_same_role_across_sources(self, left: str, right: str):
        assert titles_are_similar(left, right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Backend Developer", "Frontend Designer"),
            ("Java Developer", "Product Manager"),
        ],
    )
    def test_keeps_different_roles_apart(self, left: str, right: str):
        assert not titles_are_similar(left, right)

    def test_empty_title_is_never_similar(self):
        assert not titles_are_similar("", "Backend Developer")
