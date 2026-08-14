"""What every connector returns, regardless of where it fetched from.

Connectors do not touch the database. They return immutable value objects and
arm 1 decides what to persist, which keeps every connector testable without a
database and makes a bad source impossible to half-write into the schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from jobbot.models import SourceKind, url_fingerprint
from jobbot.net import HttpClient
from jobbot.normalize import detect_country, detect_remote


@dataclass(frozen=True, slots=True)
class RawCompany:
    name: str
    domain: str
    country: str | None = None
    source: SourceKind = SourceKind.ATS
    source_ref: str | None = None
    tech_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RawJob:
    """A posting as the source described it, before scoring or persistence."""

    company: RawCompany
    title: str
    url: str
    description: str = ""
    location: str = ""
    posted_at: datetime | None = None
    ats_provider: str | None = None
    external_id: str | None = None
    apply_url: str | None = None

    @property
    def url_hash(self) -> str:
        return url_fingerprint(self.url)

    @property
    def country(self) -> str | None:
        """Where *this posting* is, which is not where the company is.

        Stripe is a US company with roles in Dublin and Singapore. Inheriting the
        company's country here would reject every local role at a foreign
        employer and accept every foreign role at a local one.
        """
        return detect_country(self.location, self.description)

    @property
    def is_remote(self) -> bool:
        remote, _ = detect_remote(self.title, self.location, self.description)
        return remote


@runtime_checkable
class Connector(Protocol):
    """A source of postings."""

    name: str
    source: SourceKind

    async def fetch(self, client: HttpClient) -> Sequence[RawJob]: ...
