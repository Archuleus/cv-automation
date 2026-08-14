"""Rule-based match scoring.

Everything an LLM would be slow and expensive at deciding — is this a software
role, is it too senior, can the applicant legally work there — is decided here
for free and deterministically. The LLM budget is reserved for arm 3, where it
writes content, not for filtering thousands of postings.

A score is never just a number: every posting carries the reasons behind its
score so the review queue can explain itself and a bad rule is visible instead
of mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobbot.normalize import (
    Seniority,
    contains_any,
    detect_country,
    detect_remote,
    detect_seniority,
    fold,
    region_reach,
    remote_region_lock,
)
from jobbot.profile import Profile

# Component ceilings. They sum to 100 so the total needs no rescaling.
ROLE_MAX = 30
SKILLS_MAX = 35
SENIORITY_MAX = 20
GEOGRAPHY_MAX = 15

# Diminishing returns: matching six core skills already says everything a
# seventh would. Caps also stop keyword-stuffed postings from outranking honest ones.
CORE_HIT_CAP = 6
SECONDARY_HIT_CAP = 4
EXPLORATORY_HIT_CAP = 2
CORE_POINTS = 25
SECONDARY_POINTS = 7
EXPLORATORY_POINTS = 3

# Named remote scopes that still include someone working from Türkiye. Region
# words are handled by normalize.region_reach; this covers the country-level
# names that regex-extracted locks produce.
TR_INCLUSIVE_REGIONS = ("turkey", "turkiye", "tr")

SENIORITY_POINTS: dict[Seniority, int] = {
    Seniority.JUNIOR: SENIORITY_MAX,
    Seniority.MID: 18,
    Seniority.INTERN: 14,
    Seniority.UNKNOWN: 12,
}


@dataclass(frozen=True, slots=True)
class Posting:
    """The subset of a job posting that scoring actually reads."""

    title: str
    description: str = ""
    location: str = ""
    country: str | None = None


@dataclass(frozen=True, slots=True)
class MatchScore:
    total: int
    role: int = 0
    skills: int = 0
    seniority: int = 0
    geography: int = 0
    detected_seniority: Seniority = Seniority.UNKNOWN
    detected_country: str | None = None
    is_remote: bool = False
    rejected: bool = False
    reject_reason: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def explanation(self) -> str:
        if self.rejected:
            return f"rejected: {self.reject_reason}"
        return "; ".join(self.reasons)


def _rejected(reason: str, **detected: object) -> MatchScore:
    return MatchScore(total=0, rejected=True, reject_reason=reason, **detected)  # type: ignore[arg-type]


def _score_role(profile: Profile, folded_title: str) -> tuple[int, str | None, str]:
    """Best-matching role family, its points, and a human-readable reason."""
    best_weight = 0.0
    best_name: str | None = None
    for family in profile.role_families:
        if contains_any(folded_title, family.titles) and family.weight > best_weight:
            best_weight, best_name = family.weight, family.name
    if best_name is None:
        return 0, None, "title matches no target role family"
    return round(ROLE_MAX * best_weight), best_name, f"role family '{best_name}'"


def _score_skills(profile: Profile, blob: str) -> tuple[int, list[str], str]:
    core = [skill for skill in profile.skills.core if contains_any(blob, [skill])]
    secondary = [skill for skill in profile.skills.secondary if contains_any(blob, [skill])]
    exploratory = [skill for skill in profile.skills.exploratory if contains_any(blob, [skill])]

    points = round(
        CORE_POINTS * min(len(core), CORE_HIT_CAP) / CORE_HIT_CAP
        + SECONDARY_POINTS * min(len(secondary), SECONDARY_HIT_CAP) / SECONDARY_HIT_CAP
        + EXPLORATORY_POINTS * min(len(exploratory), EXPLORATORY_HIT_CAP) / EXPLORATORY_HIT_CAP
    )
    matched = core + secondary + exploratory
    reason = f"{len(core)} core skills ({', '.join(core[:4]) or 'none'})"
    return points, matched, reason


def _score_geography(
    profile: Profile, posting: Posting, is_remote: bool
) -> tuple[int, str | None, str]:
    """Points, a rejection reason if the location is unworkable, and an explanation.

    The decisive question is not "is this remote" but "can this person hold this
    job from Türkiye". A posting that says *Remote - Ontario, Canada* is remote
    and still unreachable, so a named non-Turkish country disqualifies a posting
    whether or not the word "remote" appears next to it.
    """
    geography = profile.geography
    country = posting.country or detect_country(posting.location, posting.description)

    if country == geography.primary_country:
        return GEOGRAPHY_MAX, None, f"in {country}"

    if country is not None:
        # A named foreign country: onsite means relocation, remote means the
        # role is fenced to that country's workforce. Neither is available.
        placement = "remote within" if is_remote else "onsite in"
        return 0, f"{placement} {country}, relocation not available", f"{placement} {country}"

    abroad_points = round(
        GEOGRAPHY_MAX * min(geography.remote_abroad_weight / geography.primary_weight, 1.0)
    )

    # No single country named. It may still name a region, and whether Türkiye
    # sits inside it decides the posting.
    reach = region_reach(posting.location)
    if reach == "excludes_tr":
        return 0, f"restricted to {posting.location.strip()}", "region excludes Türkiye"
    if reach == "includes_tr":
        return abroad_points, None, f"{posting.location.strip()}, open to Türkiye"

    if not is_remote:
        if posting.location.strip():
            # A location was given and it is not Türkiye and not recognisable.
            # Treating it as "maybe local" is how Budapest roles got stored.
            return 0, f"onsite at unrecognised location {posting.location!r}", "onsite abroad"
        return 3, None, "no location given, not marked remote"

    lock = remote_region_lock(posting.title, posting.location, posting.description)
    if lock and region_reach(lock) != "includes_tr" and not contains_any(
        fold(lock), TR_INCLUSIVE_REGIONS
    ):
        return 0, f"remote but restricted to {lock}", f"remote, locked to {lock}"

    # Genuinely open remote. Scaled by the applicant's stated split so the queue
    # naturally lands near the intended primary/abroad ratio.
    return abroad_points, None, "remote, open to Türkiye"


def score_posting(posting: Posting, profile: Profile) -> MatchScore:
    """Score a posting 0-100, or reject it outright."""
    folded_title = fold(posting.title)
    blob = fold(" ".join((posting.title, posting.description[:6000])))

    is_remote, is_hybrid = detect_remote(posting.title, posting.location, posting.description)
    seniority = detect_seniority(posting.title, posting.description)
    country = posting.country or detect_country(posting.location, posting.description)
    detected: dict[str, object] = {
        "detected_seniority": seniority,
        "detected_country": country,
        "is_remote": is_remote,
    }

    # Exclusions are matched against the title alone. Matching the description
    # rejects an engineering role for the words "our sales team" -- in a live
    # run that alone threw away 2,477 of 8,268 postings.
    if contains_any(folded_title, profile.excluded_keywords):
        hit = next(kw for kw in profile.excluded_keywords if contains_any(folded_title, [kw]))
        return _rejected(f"excluded keyword '{hit}'", **detected)

    if seniority.value in profile.seniority_excluded:
        return _rejected(f"seniority '{seniority.value}' above target", **detected)

    role_points, role_name, role_reason = _score_role(profile, folded_title)
    skill_points, matched_skills, skill_reason = _score_skills(profile, blob)

    # Neither a recognised title nor meaningful skill overlap: not a software role.
    if role_name is None and len(matched_skills) < 2:
        return _rejected("no role or skill overlap", **detected)

    geo_points, geo_rejection, geo_reason = _score_geography(profile, posting, is_remote)
    if geo_rejection:
        return _rejected(geo_rejection, **detected)

    seniority_points = SENIORITY_POINTS.get(seniority, SENIORITY_MAX)
    reasons = [
        role_reason,
        skill_reason,
        f"seniority '{seniority.value}'",
        geo_reason + (" (hybrid)" if is_hybrid else ""),
    ]

    return MatchScore(
        total=min(100, role_points + skill_points + seniority_points + geo_points),
        role=role_points,
        skills=skill_points,
        seniority=seniority_points,
        geography=geo_points,
        reasons=tuple(reasons),
        **detected,  # type: ignore[arg-type]
    )
