"""Company discovery: growing the list of employers worth investigating.

This runs before arm 2 and feeds it. Everything it finds lands in the
`companies` table with `investigated_at` unset, and arm 2 works through whatever
is unvisited -- so a source that finds two thousand companies does not require
re-visiting the two hundred already done.

The bias this corrects is the point. A hand-written list contains companies
famous enough to be remembered, which are the ones that receive thousands of
applications and rarely hire juniors. The companies worth finding are the ones
nobody thought to write down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from jobbot import events
from jobbot.config import Settings, get_settings
from jobbot.discovery.base import CompanySource, DiscoveredCompany, deduplicate
from jobbot.discovery.directories import DirectorySource
from jobbot.discovery.github_orgs import GitHubOrgSource
from jobbot.models import Company
from jobbot.net import HttpClient

logger = logging.getLogger("jobbot.arms.companies")


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    found: int = 0
    added: int = 0
    already_known: int = 0
    per_source: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return f"found={self.found} added={self.added} known={self.already_known}"


def default_sources(settings: Settings | None = None) -> list[CompanySource]:
    """Every source enabled by the current configuration."""
    settings = settings or get_settings()
    return [
        DirectorySource(),
        GitHubOrgSource(token=settings.github_token),
    ]


def store(session: Session, companies: list[DiscoveredCompany]) -> tuple[int, int]:
    """Insert companies we do not already have. Returns ``(added, known)``."""
    added = known = 0
    for company in companies:
        existing = session.exec(
            select(Company).where(Company.domain == company.domain)
        ).first()
        if existing is not None:
            known += 1
            continue

        record = Company(
            name=company.name,
            domain=company.domain,
            country=company.country,
            source=company.source,
            source_ref=company.source_ref,
        )
        session.add(record)
        session.flush()
        added += 1
        events.record(
            session,
            entity_type="company",
            entity_id=record.id,
            event="discovered",
            domain=company.domain,
            source=company.source_ref or str(company.source),
        )
    return added, known


async def run(
    *,
    session: Session,
    settings: Settings | None = None,
    sources: list[CompanySource] | None = None,
    client: HttpClient | None = None,
) -> DiscoveryReport:
    settings = settings or get_settings()
    sources = sources if sources is not None else default_sources(settings)

    owns_client = client is None
    client = client or HttpClient(settings)

    per_source: dict[str, int] = {}
    everything: list[DiscoveredCompany] = []
    try:
        for source in sources:
            try:
                found = list(await source.discover(client))
            except Exception as error:
                # One dead source must not cost the others their results.
                logger.warning("source %s failed: %s", source.name, error)
                per_source[source.name] = 0
                continue
            per_source[source.name] = len(found)
            everything.extend(found)
            logger.info("source %s: %d companies", source.name, len(found))
    finally:
        if owns_client:
            await client.aclose()

    unique = deduplicate(everything)
    added, known = store(session, unique)
    session.commit()

    report = DiscoveryReport(
        found=len(unique), added=added, already_known=known, per_source=per_source
    )
    events.record(
        session,
        entity_type="run",
        entity_id=None,
        event="company_discovery_completed",
        found=report.found,
        added=report.added,
    )
    session.commit()
    logger.info("company discovery complete: %s", report.summary)
    return report
