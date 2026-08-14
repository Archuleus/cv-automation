"""The applicant profile that arm 1 scores jobs against.

``cv/profile.json`` is personal data and stays out of version control. It is
loaded once and validated strictly: a typo in the profile would silently skew
every match score, so unknown fields are rejected rather than ignored.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jobbot.config import PROJECT_ROOT

DEFAULT_PROFILE_PATH = PROJECT_ROOT / "cv" / "profile.json"


class ProfileNotFoundError(FileNotFoundError):
    """Raised when no profile has been created yet."""


class StrictModel(BaseModel):
    """Rejects unknown fields, but treats `_`-prefixed keys as comments.

    Unknown fields are refused because a typo in the profile would otherwise
    skew every match score silently. JSON has no comment syntax though, and a
    profile a person fills in by hand badly needs one -- so keys beginning with
    an underscore are dropped before validation. A real typo (`seniorty_targets`)
    still fails loudly; only a deliberate `_note` passes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_comment_keys(cls, data: object) -> object:
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(key, str) and key.startswith("_"))
            }
        return data


class Geography(StrictModel):
    primary_country: str = "TR"
    primary_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    remote_abroad_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    onsite_countries: tuple[str, ...] = ("TR",)
    preferred_tr_cities: tuple[str, ...] = ()
    requires_visa_sponsorship_abroad: bool = True

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Geography:
        total = self.primary_weight + self.remote_abroad_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"primary_weight + remote_abroad_weight must equal 1.0, got {total}"
            )
        return self


class CvVariant(StrictModel):
    file: str
    language: Literal["tr", "en"]
    emphasis: tuple[str, ...] = ()


class Skills(StrictModel):
    # Tiered because a job matching a core skill is worth far more than one
    # matching something the applicant has only played with.
    core: tuple[str, ...]
    secondary: tuple[str, ...] = ()
    exploratory: tuple[str, ...] = ()

    @field_validator("core", "secondary", "exploratory", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value]
        return value

    @property
    def all(self) -> frozenset[str]:
        return frozenset(self.core + self.secondary + self.exploratory)


class Experience(StrictModel):
    """One real job, with what was actually built in it.

    Without these, a model writing an application has only a skill list to work
    from and fills the gap by inventing plausible projects. Concrete highlights
    are the cheapest defence against that.
    """

    role: str
    company: str
    location: str = ""
    period: str = ""
    highlights: tuple[str, ...] = ()


class Project(StrictModel):
    name: str
    summary: str = ""
    highlights: tuple[str, ...] = ()


class RoleFamily(StrictModel):
    """A group of job titles the applicant would accept, and how much they want it.

    A title ending in ``*`` matches any suffix. Turkish is agglutinative, so
    ``"yazılım mühendis*"`` is what covers *mühendisi*, *mühendisliği* and
    *mühendislik*; without the wildcard most real Turkish postings slip through.
    """

    name: str
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    titles: tuple[str, ...]

    @field_validator("titles", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value]
        return value


class Profile(StrictModel):
    name: str
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    website: str = ""
    linkedin: str = ""
    github: str = ""

    graduation_year: int | None = None
    years_of_experience: float = 0
    seniority_targets: tuple[str, ...] = ("junior", "entry", "mid")
    seniority_excluded: tuple[str, ...] = ("senior", "staff", "principal", "lead")

    spoken_languages: dict[str, str] = Field(default_factory=dict)
    geography: Geography = Field(default_factory=Geography)
    cv_variants: dict[str, CvVariant] = Field(default_factory=dict)
    education: str = ""
    experience: tuple[Experience, ...] = ()
    projects: tuple[Project, ...] = ()
    skills: Skills
    role_families: tuple[RoleFamily, ...]
    excluded_keywords: tuple[str, ...] = ()

    @field_validator("seniority_targets", "seniority_excluded", "excluded_keywords", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value]
        return value

    @model_validator(mode="after")
    def _seniority_sets_are_disjoint(self) -> Profile:
        overlap = set(self.seniority_targets) & set(self.seniority_excluded)
        if overlap:
            raise ValueError(f"seniority appears in both targets and excluded: {sorted(overlap)}")
        return self

    @property
    def links(self) -> tuple[tuple[str, str], ...]:
        """Labelled links for the signature block of an application.

        Ordered by what a reviewer is most likely to open: the portfolio shows
        finished work, GitHub shows code, LinkedIn shows history. Empty fields
        are dropped so a half-filled profile never emits a bare label.
        """
        candidates = (
            ("Portfolio", self.website),
            ("GitHub", self.github),
            ("LinkedIn", self.linkedin),
        )
        return tuple((label, url) for label, url in candidates if url.strip())

    def cv_for(self, language: str) -> tuple[str, CvVariant] | None:
        """Pick a CV variant matching the posting's language, falling back to any."""
        for key, variant in self.cv_variants.items():
            if variant.language == language:
                return key, variant
        return next(iter(self.cv_variants.items()), None)


def load_profile(path: Path | None = None) -> Profile:
    path = path or DEFAULT_PROFILE_PATH
    if not path.exists():
        raise ProfileNotFoundError(
            f"no applicant profile at {path}. Copy cv/profile.example.json to "
            "cv/profile.json and fill it in."
        )
    return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def get_profile() -> Profile:
    return load_profile()
