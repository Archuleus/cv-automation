"""Arm 1: find companies and open roles, score them, store what is worth pursuing.

The order matters. Deduplication happens before scoring because scoring is the
expensive part, and rejection happens before persistence because a database of
mostly-rejected rows is a database nobody will look at. What lands in ``jobs``
is only what arm 2 should spend requests on.

Rejected postings are still counted and logged -- silently dropping them would
make a bad scoring rule invisible.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlmodel import Session, select

from jobbot import events
from jobbot.config import Settings, get_settings
from jobbot.connectors.base import Connector, RawJob
from jobbot.db import session_scope
from jobbot.models import Company, Job, JobStatus, SourceKind
from jobbot.net import HttpClient
from jobbot.normalize import titles_are_similar
from jobbot.profile import Profile, get_profile
from jobbot.scoring import Posting, score_posting

logger = logging.getLogger("jobbot.arms.discover")


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    fetched: int = 0
    duplicates: int = 0
    rejected: int = 0
    below_threshold: int = 0
    already_known: int = 0
    stored: int = 0
    blocked_companies: int = 0
    per_connector: dict[str, int] = field(default_factory=dict)
    reject_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return (
            f"fetched={self.fetched} duplicates={self.duplicates} "
            f"rejected={self.rejected} below_threshold={self.below_threshold} "
            f"already_known={self.already_known} stored={self.stored}"
        )


# Hosts that belong to an ATS vendor rather than to the hiring company. A
# posting URL on one of these says nothing about who is hiring.
ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "recruitee.com",
    "smartrecruiters.com",
)


def company_domain(job: RawJob) -> str:
    """A stable identity for the hiring company.

    The domain is the natural key, but it is not always known. Falling back to
    the posting's host is only safe when that host belongs to the company: for
    an ATS posting the host is `boards.greenhouse.io`, so every company without
    a configured domain would collapse into a single row under the unique
    constraint on `companies.domain`.
    """
    if job.company.domain:
        return job.company.domain.lower().removeprefix("www.")

    host = urlparse(job.url).netloc.lower().removeprefix("www.")
    if any(host == vendor or host.endswith(f".{vendor}") for vendor in ATS_HOSTS):
        # Fall back to the board identity, which is unique per company.
        return job.company.source_ref or host
    return host


def deduplicate(jobs: Iterable[RawJob]) -> tuple[list[RawJob], int]:
    """Collapse the same posting arriving from several sources.

    Exact URL matches are cheap and catch most of it. The fuzzy pass catches the
    rest: the same role listed under a company's own domain and again on an
    aggregator, with a slightly different title.
    """
    unique: list[RawJob] = []
    seen_urls: set[str] = set()
    by_company: dict[str, list[str]] = {}
    duplicates = 0

    for job in jobs:
        if job.url_hash in seen_urls:
            duplicates += 1
            continue

        domain = company_domain(job)
        titles = by_company.setdefault(domain, [])
        if any(titles_are_similar(job.title, known) for known in titles):
            duplicates += 1
            continue

        seen_urls.add(job.url_hash)
        titles.append(job.title)
        unique.append(job)

    return unique, duplicates


def _get_or_create_company(session: Session, job: RawJob) -> Company | None:
    """Return the company row, or None if it has been blocked by the user."""
    domain = company_domain(job)
    existing = session.exec(select(Company).where(Company.domain == domain)).first()
    if existing is not None:
        return None if existing.is_blocked else existing

    company = Company(
        name=job.company.name or domain,
        domain=domain,
        country=job.company.country,
        source=job.company.source,
        source_ref=job.company.source_ref,
        tech_tags=list(job.company.tech_tags),
    )
    session.add(company)
    session.flush()
    events.record(
        session,
        entity_type="company",
        entity_id=company.id,
        event="discovered",
        domain=domain,
        source=str(job.company.source),
    )
    return company


def persist(
    session: Session,
    jobs: Sequence[RawJob],
    profile: Profile,
    settings: Settings,
    report: DiscoveryReport,
) -> DiscoveryReport:
    """Score each posting and store the ones worth pursuing."""
    rejected = dict(report.reject_reasons)
    counters = {
        "rejected": report.rejected,
        "below_threshold": report.below_threshold,
        "already_known": report.already_known,
        "stored": report.stored,
        "blocked_companies": report.blocked_companies,
    }

    for job in jobs:
        if session.exec(select(Job).where(Job.url_hash == job.url_hash)).first() is not None:
            counters["already_known"] += 1
            continue

        score = score_posting(
            Posting(
                title=job.title,
                description=job.description,
                location=job.location,
                country=job.country,
            ),
            profile,
        )

        if score.rejected:
            counters["rejected"] += 1
            reason = score.reject_reason or "unspecified"
            # Group by rule, not by the specific keyword that fired.
            key = reason.split("'")[0].strip() or reason
            rejected[key] = rejected.get(key, 0) + 1
            continue

        if score.total < settings.min_match_score:
            counters["below_threshold"] += 1
            continue

        company = _get_or_create_company(session, job)
        if company is None:
            counters["blocked_companies"] += 1
            continue

        assert company.id is not None  # flushed above
        record = Job(
            company_id=company.id,
            title=job.title,
            url=job.url,
            url_hash=job.url_hash,
            location=job.location,
            is_remote=job.is_remote,
            seniority=score.detected_seniority.value,
            posted_at=job.posted_at,
            raw_description=job.description or None,
            ats_provider=job.ats_provider,
            external_id=job.external_id or None,
            match_score=score.total,
            match_reason=score.explanation,
            status=JobStatus.CONTACT_PENDING,
        )
        session.add(record)
        session.flush()
        counters["stored"] += 1
        events.record(
            session,
            entity_type="job",
            entity_id=record.id,
            event="scored",
            score=score.total,
            reason=score.explanation,
        )

    return DiscoveryReport(
        fetched=report.fetched,
        duplicates=report.duplicates,
        per_connector=report.per_connector,
        reject_reasons=rejected,
        **counters,
    )


async def collect(connectors: Sequence[Connector], client: HttpClient) -> tuple[list[RawJob], dict]:
    """Run every connector, tolerating individual failures."""
    collected: list[RawJob] = []
    per_connector: dict[str, int] = {}

    for connector in connectors:
        try:
            jobs = list(await connector.fetch(client))
        except Exception:
            # A broken source must not take down the run; the next scheduled
            # pass will pick it up if it recovers.
            logger.exception("connector %s failed", connector.name)
            per_connector[connector.name] = 0
            continue
        per_connector[connector.name] = len(jobs)
        collected.extend(jobs)
        logger.info("connector %s returned %d postings", connector.name, len(jobs))

    return collected, per_connector


async def run(
    connectors: Sequence[Connector],
    *,
    settings: Settings | None = None,
    profile: Profile | None = None,
    client: HttpClient | None = None,
) -> DiscoveryReport:
    settings = settings or get_settings()
    profile = profile or get_profile()

    owns_client = client is None
    client = client or HttpClient(settings)
    try:
        collected, per_connector = await collect(connectors, client)
    finally:
        if owns_client:
            await client.aclose()

    unique, duplicates = deduplicate(collected)
    report = DiscoveryReport(
        fetched=len(collected), duplicates=duplicates, per_connector=per_connector
    )

    with session_scope() as session:
        report = persist(session, unique, profile, settings, report)
        events.record(
            session,
            entity_type="run",
            entity_id=None,
            event="discovery_completed",
            **{
                "fetched": report.fetched,
                "stored": report.stored,
                "rejected": report.rejected,
            },
        )

    logger.info("discovery complete: %s", report.summary)
    return report


__all__ = ["DiscoveryReport", "SourceKind", "collect", "deduplicate", "persist", "run"]
