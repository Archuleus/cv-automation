"""Arm 2: find a way to actually apply to a company.

Ordered by what each answer is worth, not by what is easiest to fetch:

1. **An ATS token** is the best outcome by a wide margin. A careers page that
   redirects to Greenhouse or Lever hands us a machine-readable feed of that
   company's openings forever, which arm 1 then polls on every run. One page
   fetch turns into a permanent source.
2. **A careers page** gives a human somewhere to go.
3. **A published hiring address** gives a way to write to them.
4. **Assisted search links** cost no requests and are produced for everyone,
   because for a company that posts only on kariyer.net that is the whole answer.

The Turkish market is why this arm exists: almost none of these employers use
the international ATSs arm 1 reads, so without this step they are invisible.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, col, select

from jobbot import events
from jobbot.config import Settings, get_settings
from jobbot.connectors.ats import Board, detect_board
from jobbot.connectors.boards import load_boards, merge_boards, save_boards
from jobbot.contacts.emails import contact_page_urls, extract_emails
from jobbot.contacts.pages import (
    candidate_urls,
    find_career_links,
    is_careers_hub,
    looks_like_careers_page,
)
from jobbot.models import Company, Contact, ContactKind, SourceKind
from jobbot.net import FetchError, HttpClient, RequestKind

logger = logging.getLogger("jobbot.arms.contact")

def seed_path() -> Path:
    return get_settings().tr_companies_file

# Companies are independent, so several can be in flight at once. The per-domain
# rate limiter still serialises requests to any single site.
COMPANY_CONCURRENCY = 6
# Guessing paths is cheap but not free; stop as soon as one looks real.
MAX_PATH_GUESSES = 8
MAX_CONTACT_PAGES = 3


@dataclass(frozen=True, slots=True)
class SeedCompany:
    name: str
    domain: str
    sector: str = ""


@dataclass(frozen=True, slots=True)
class CompanyFindings:
    """What one company's site gave up."""

    company: SeedCompany
    careers_url: str = ""
    board: Board | None = None
    emails: tuple[tuple[str, float], ...] = ()
    error: str = ""

    @property
    def outcome(self) -> str:
        if self.board is not None:
            return "ats detected"
        if self.emails:
            return "email found"
        if self.careers_url:
            return "careers page only"
        return self.error or "nothing found"


@dataclass(frozen=True, slots=True)
class ContactReport:
    processed: int = 0
    careers_pages: int = 0
    ats_detected: int = 0
    emails_found: int = 0
    boards_added: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return (
            f"processed={self.processed} careers={self.careers_pages} "
            f"ats={self.ats_detected} emails={self.emails_found} "
            f"boards_added={self.boards_added}"
        )


def pending_companies(session: Session, limit: int | None = None) -> list[SeedCompany]:
    """Companies in the database that arm 2 has not visited yet.

    Reading from the database rather than a file is what lets discovery
    compound: a source can add two thousand employers and this picks up only
    the new ones, without re-crawling everything already done.
    """
    query = (
        select(Company)
        .where(col(Company.investigated_at).is_(None))
        .where(col(Company.is_blocked).is_(False))
        .order_by(col(Company.discovered_at))
    )
    if limit is not None:
        query = query.limit(limit)
    return [
        SeedCompany(name=company.name, domain=company.domain, sector=company.source_ref or "")
        for company in session.exec(query).all()
    ]


def bootstrap(session: Session, path: Path | None = None) -> int:
    """Load the static seed file into the database, once.

    The file is a starting point, not the source of truth. After the first run
    the database holds every company, including the ones discovery found.
    """
    added = 0
    for entry in load_seed(path):
        existing = session.exec(select(Company).where(Company.domain == entry.domain)).first()
        if existing is not None:
            continue
        session.add(
            Company(
                name=entry.name,
                domain=entry.domain,
                country="TR",
                source=SourceKind.TR_REGISTRY,
                source_ref=entry.sector,
            )
        )
        added += 1
    session.commit()
    return added


def load_seed(path: Path | None = None) -> tuple[SeedCompany, ...]:
    path = path or seed_path()
    if not path.exists():
        logger.warning("no seed company list at %s", path)
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("companies", payload) if isinstance(payload, dict) else payload
    return tuple(
        SeedCompany(
            name=str(entry["name"]),
            domain=str(entry["domain"]).lower().removeprefix("www."),
            sector=str(entry.get("sector", "")),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") and entry.get("domain")
    )


async def _fetch(client: HttpClient, url: str) -> str:
    """Fetch a page, treating every failure as "no page here".

    Timeouts, TLS errors, robots refusals and 404s all mean the same thing to
    this arm, and none of them should stop the other 130 companies.
    """
    try:
        # One attempt: this is a guess, and a refused connection means the page
        # is not there, not that the server is briefly busy.
        return await client.get_text(url, kind=RequestKind.PAGE, attempts=1)
    except (FetchError, Exception) as error:
        logger.debug("could not read %s: %s", url, error)
        return ""


async def _find_careers_page(client: HttpClient, company: SeedCompany) -> tuple[str, str]:
    """Return ``(url, html)`` for the careers page, or empty strings."""
    for url in candidate_urls(company.domain)[:MAX_PATH_GUESSES]:
        html = await _fetch(client, url)
        if html and is_careers_hub(html, url=url, domain=company.domain):
            return url, html

    # No guess landed. The homepage knows where its own careers page is.
    home = f"https://{company.domain}"
    homepage = await _fetch(client, home)
    if not homepage:
        return "", ""

    for link in find_career_links(homepage, base_url=home, domain=company.domain)[:3]:
        # An off-site link is usually an ATS, which is worth following even
        # though it will not look like a careers page to the text heuristic.
        if detect_board(link.url) is not None:
            return link.url, ""
        html = await _fetch(client, link.url)
        if html and looks_like_careers_page(html):
            return link.url, html
    return "", ""


async def investigate(client: HttpClient, company: SeedCompany) -> CompanyFindings:
    """Everything one company's public site will tell us."""
    careers_url, careers_html = await _find_careers_page(client, company)
    if not careers_url:
        return CompanyFindings(company=company, error="no careers page")

    # The best possible outcome: a permanent, machine-readable job feed.
    board = detect_board(careers_url)
    if board is None and careers_html:
        for link in find_career_links(
            careers_html, base_url=careers_url, domain=company.domain
        ):
            board = detect_board(link.url)
            if board is not None:
                break
    if board is not None:
        board = Board(
            provider=board.provider,
            token=board.token,
            name=company.name,
            domain=company.domain,
            country="TR",
        )

    found = extract_emails(
        careers_html, company_domain=company.domain, source_url=careers_url
    )
    if not found:
        for url in contact_page_urls(company.domain)[:MAX_CONTACT_PAGES]:
            html = await _fetch(client, url)
            found = extract_emails(html, company_domain=company.domain, source_url=url)
            if found:
                break

    return CompanyFindings(
        company=company,
        careers_url=careers_url,
        board=board,
        emails=tuple((item.address, item.confidence) for item in found[:3]),
    )


def persist(session: Session, findings: CompanyFindings) -> Company:
    """Record what was found, without duplicating anything already known."""
    seed = findings.company
    company = session.exec(select(Company).where(Company.domain == seed.domain)).first()
    if company is None:
        company = Company(
            name=seed.name,
            domain=seed.domain,
            country="TR",
            source=SourceKind.TR_REGISTRY,
            source_ref=seed.sector,
        )
        session.add(company)
        session.flush()
        events.record(
            session,
            entity_type="company",
            entity_id=company.id,
            event="discovered",
            domain=seed.domain,
            source=str(SourceKind.TR_REGISTRY),
        )

    assert company.id is not None
    known = {
        (contact.kind, contact.value)
        for contact in session.exec(
            select(Contact).where(Contact.company_id == company.id)
        ).all()
    }

    def add(kind: ContactKind, value: str, confidence: float, via: str) -> None:
        if not value or (kind, value) in known:
            return
        session.add(
            Contact(
                company_id=company.id,
                kind=kind,
                value=value,
                confidence=confidence,
                discovered_via=via,
            )
        )
        known.add((kind, value))

    if findings.board is not None:
        add(
            ContactKind.ATS_FORM,
            f"{findings.board.provider}:{findings.board.token}",
            1.0,
            "career page redirect",
        )
    if findings.careers_url:
        add(ContactKind.CAREER_PAGE, findings.careers_url, 0.7, "site crawl")
    for address, confidence in findings.emails:
        add(ContactKind.EMAIL, address, confidence, "published on site")

    # Marking the visit is what stops the next run repeating it, and is why a
    # crawl of ten thousand companies can be spread across many runs.
    company.investigated_at = datetime.now(UTC)
    session.add(company)
    session.flush()
    events.record(
        session,
        entity_type="company",
        entity_id=company.id,
        event="contact_resolved",
        outcome=findings.outcome,
    )
    return company


async def run(
    *,
    settings: Settings | None = None,
    seed: tuple[SeedCompany, ...] | None = None,
    session: Session,
    limit: int | None = None,
    client: HttpClient | None = None,
) -> ContactReport:
    settings = settings or get_settings()
    if seed is not None:
        companies = list(seed)[:limit] if limit else list(seed)
    else:
        bootstrap(session)
        companies = pending_companies(session, limit)

    owns_client = client is None
    client = client or HttpClient(settings)
    semaphore = asyncio.Semaphore(COMPANY_CONCURRENCY)

    async def one(company: SeedCompany) -> CompanyFindings:
        async with semaphore:
            try:
                return await investigate(client, company)
            except Exception as error:
                # One unreachable or malformed site must not end the run.
                logger.warning("investigating %s failed: %s", company.domain, error)
                return CompanyFindings(company=company, error="investigation failed")

    outcomes: dict[str, int] = {}
    discovered_boards: list[Board] = []
    careers = ats = emails = 0
    processed = 0

    # Results are handled as each site finishes rather than after all of them,
    # so a ten-minute crawl that dies at company 120 keeps the first 119. The
    # database is touched only here, in one coroutine, so the concurrency above
    # never shares a session.
    tasks = [asyncio.create_task(one(company)) for company in companies]
    try:
        for completed in asyncio.as_completed(tasks):
            findings = await completed
            processed += 1
            outcomes[findings.outcome] = outcomes.get(findings.outcome, 0) + 1
            if findings.careers_url:
                careers += 1
            if findings.board is not None:
                ats += 1
                discovered_boards.append(findings.board)
            if findings.emails:
                emails += 1
            persist(session, findings)
            session.commit()
            logger.info(
                "[%d/%d] %s: %s",
                processed,
                len(companies),
                findings.company.name,
                findings.outcome,
            )
    finally:
        for task in tasks:
            task.cancel()
        if owns_client:
            await client.aclose()

    # Feed every detected board back into arm 1, so a company found here starts
    # contributing its own postings on the next discovery run.
    added = 0
    if discovered_boards:
        existing = load_boards()
        merged = merge_boards(existing, discovered_boards)
        added = len(merged) - len(existing)
        if added:
            save_boards(merged)
            logger.info("added %d board(s) to the registry", added)

    report = ContactReport(
        processed=processed,
        careers_pages=careers,
        ats_detected=ats,
        emails_found=emails,
        boards_added=added,
        outcomes=outcomes,
    )
    events.record(
        session,
        entity_type="run",
        entity_id=None,
        event="contact_resolution_completed",
        processed=report.processed,
        careers=report.careers_pages,
        emails=report.emails_found,
    )
    logger.info("arm 2 complete: %s", report.summary)
    return report
