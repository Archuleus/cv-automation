"""Applicant Tracking System job boards.

Every ATS in here publishes a free, public, documented JSON endpoint for its
customers' job boards -- the same feed that renders the company's own careers
page. Reading it is not scraping, it does not break when the page is restyled,
and it hands us the canonical application URL for arm 3.

All six providers share one shape: a board token identifies a company, one GET
returns that company's open roles. So the fetching, error handling and
normalisation live here once, and each provider contributes only its URL
template and a field mapping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jobbot.connectors.base import RawCompany, RawJob
from jobbot.models import SourceKind
from jobbot.net import FetchError, HttpClient, RequestKind

logger = logging.getLogger("jobbot.connectors.ats")

# Postings are fetched concurrently across companies, but the per-domain rate
# limiter still serialises calls to any single host.
BOARD_CONCURRENCY = 8
DESCRIPTION_LIMIT = 20_000


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
    return None


def _strip_html(value: Any) -> str:
    """ATS descriptions are HTML; scoring only needs the words.

    Unescaping comes first: Greenhouse returns the markup HTML-escaped inside
    JSON, so stripping tags before unescaping leaves real tags behind in the
    text and pollutes every skill match with markup.
    """
    import html
    import re

    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # A second pass catches entities that were escaped twice (&amp;nbsp;).
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()[:DESCRIPTION_LIMIT]


@dataclass(frozen=True, slots=True)
class Board:
    """One company's board on one ATS."""

    provider: str
    token: str
    name: str
    domain: str
    country: str | None = None

    def as_company(self) -> RawCompany:
        return RawCompany(
            name=self.name,
            domain=self.domain,
            country=self.country,
            source=SourceKind.ATS,
            source_ref=f"{self.provider}:{self.token}",
        )


@dataclass(frozen=True, slots=True)
class AtsProvider:
    """URL template plus field mapping for one ATS."""

    name: str
    url_template: str
    # Pulls the list of postings out of whatever envelope the provider uses.
    extract: Callable[[Any], Iterable[dict[str, Any]]]
    # Turns one posting into a RawJob for the given board.
    map_job: Callable[[dict[str, Any], Board], RawJob | None]
    # URL shapes that reveal a company's board token, used by arm 2.
    url_patterns: tuple[str, ...] = field(default_factory=tuple)

    def endpoint(self, token: str) -> str:
        return self.url_template.format(token=token)


# --------------------------------------------------------------------------
# Provider definitions
# --------------------------------------------------------------------------


def _greenhouse(posting: dict[str, Any], board: Board) -> RawJob | None:
    url = _first(posting, "absolute_url")
    if not url:
        return None
    location = _text((posting.get("location") or {}).get("name"))
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "title"),
        url=url,
        description=_strip_html(posting.get("content")),
        location=location,
        posted_at=_parse_time(posting.get("updated_at") or posting.get("first_published")),
        ats_provider=board.provider,
        external_id=str(posting.get("id") or ""),
        apply_url=url,
    )


def _lever(posting: dict[str, Any], board: Board) -> RawJob | None:
    url = _first(posting, "hostedUrl", "applyUrl")
    if not url:
        return None
    categories = posting.get("categories") or {}
    created = posting.get("createdAt")  # Lever reports epoch milliseconds
    posted = (
        datetime.fromtimestamp(created / 1000, tz=UTC)
        if isinstance(created, int | float)
        else None
    )
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "text"),
        url=url,
        description=_strip_html(posting.get("descriptionPlain") or posting.get("description")),
        location=_text(categories.get("location")),
        posted_at=posted,
        ats_provider=board.provider,
        external_id=str(posting.get("id") or ""),
        apply_url=_first(posting, "applyUrl", "hostedUrl"),
    )


def _ashby(posting: dict[str, Any], board: Board) -> RawJob | None:
    url = _first(posting, "jobUrl", "applyUrl")
    if not url:
        return None
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "title"),
        url=url,
        description=_strip_html(posting.get("descriptionPlain") or posting.get("descriptionHtml")),
        location=_first(posting, "location", "locationName"),
        posted_at=_parse_time(posting.get("publishedAt") or posting.get("updatedAt")),
        ats_provider=board.provider,
        external_id=str(posting.get("id") or ""),
        apply_url=_first(posting, "applyUrl", "jobUrl"),
    )


def _workable(posting: dict[str, Any], board: Board) -> RawJob | None:
    shortcode = _first(posting, "shortcode")
    url = _first(posting, "url", "application_url") or (
        f"https://apply.workable.com/{board.token}/j/{shortcode}/" if shortcode else ""
    )
    if not url:
        return None
    location = ", ".join(
        part for part in (_first(posting, "city"), _first(posting, "country")) if part
    )
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "title"),
        url=url,
        description=_strip_html(posting.get("description")),
        location=location or _first(posting, "location"),
        posted_at=_parse_time(posting.get("published_on") or posting.get("created_at")),
        ats_provider=board.provider,
        external_id=shortcode or str(posting.get("id") or ""),
        apply_url=url,
    )


def _recruitee(posting: dict[str, Any], board: Board) -> RawJob | None:
    url = _first(posting, "careers_url", "careers_apply_url")
    if not url:
        return None
    location = ", ".join(
        part for part in (_first(posting, "city"), _first(posting, "country")) if part
    )
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "title"),
        url=url,
        description=_strip_html(posting.get("description")),
        location=location or _first(posting, "location"),
        posted_at=_parse_time(posting.get("published_at") or posting.get("created_at")),
        ats_provider=board.provider,
        external_id=str(posting.get("id") or ""),
        apply_url=_first(posting, "careers_apply_url", "careers_url"),
    )


def _smartrecruiters(posting: dict[str, Any], board: Board) -> RawJob | None:
    posting_id = str(posting.get("id") or "")
    url = _first(posting, "applyUrl", "postingUrl") or (
        f"https://jobs.smartrecruiters.com/{board.token}/{posting_id}" if posting_id else ""
    )
    if not url:
        return None
    location_data = posting.get("location") or {}
    location = ", ".join(
        part
        for part in (_text(location_data.get("city")), _text(location_data.get("country")))
        if part
    )
    return RawJob(
        company=board.as_company(),
        title=_first(posting, "name", "title"),
        # The list endpoint omits descriptions; scoring falls back to the title
        # plus whatever the department and location imply.
        description=_strip_html(posting.get("jobAd")),
        url=url,
        location=location,
        posted_at=_parse_time(posting.get("releasedDate") or posting.get("createdOn")),
        ats_provider=board.provider,
        external_id=posting_id,
        apply_url=url,
    )


def _envelope(*keys: str) -> Callable[[Any], Iterable[dict[str, Any]]]:
    """Pull a list of postings out of a JSON envelope, tolerating bare lists."""

    def extract(payload: Any) -> Iterable[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    return extract


PROVIDERS: dict[str, AtsProvider] = {
    provider.name: provider
    for provider in (
        AtsProvider(
            name="greenhouse",
            url_template="https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
            extract=_envelope("jobs"),
            map_job=_greenhouse,
            url_patterns=(
                r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
                r"job-boards\.greenhouse\.io/([a-z0-9_-]+)",
            ),
        ),
        AtsProvider(
            name="lever",
            url_template="https://api.lever.co/v0/postings/{token}?mode=json",
            extract=_envelope("data"),
            map_job=_lever,
            url_patterns=(r"jobs\.lever\.co/([a-z0-9_-]+)",),
        ),
        AtsProvider(
            name="ashby",
            url_template="https://api.ashbyhq.com/posting-api/job-board/{token}",
            extract=_envelope("jobs"),
            map_job=_ashby,
            url_patterns=(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)",),
        ),
        AtsProvider(
            name="workable",
            url_template="https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
            extract=_envelope("jobs"),
            map_job=_workable,
            url_patterns=(
                r"apply\.workable\.com/([a-z0-9_-]+)",
                r"([a-z0-9_-]+)\.workable\.com",
            ),
        ),
        AtsProvider(
            name="recruitee",
            url_template="https://{token}.recruitee.com/api/offers/",
            extract=_envelope("offers"),
            map_job=_recruitee,
            url_patterns=(r"([a-z0-9_-]+)\.recruitee\.com",),
        ),
        AtsProvider(
            name="smartrecruiters",
            url_template="https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100",
            extract=_envelope("content"),
            map_job=_smartrecruiters,
            url_patterns=(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)",),
        ),
    )
}


def detect_board(url: str) -> Board | None:
    """Recognise an ATS board token from a careers URL.

    Arm 2 uses this: a company's careers page is very often just a redirect to
    its ATS, and spotting that turns "we found a careers page" into "we found
    the exact application endpoint".
    """
    import re

    for provider in PROVIDERS.values():
        for pattern in provider.url_patterns:
            match = re.search(pattern, url, flags=re.IGNORECASE)
            if match:
                token = match.group(1)
                # These subdomains are the vendor's own, not a customer board.
                if token.lower() in {"www", "api", "apply", "jobs", "help", "support"}:
                    continue
                return Board(provider=provider.name, token=token, name=token, domain="")
    return None


class AtsConnector:
    """Fetches every configured board for a single ATS provider."""

    source = SourceKind.ATS

    def __init__(self, provider: AtsProvider, boards: Sequence[Board]) -> None:
        self.provider = provider
        self.boards = tuple(boards)
        self.name = f"ats:{provider.name}"

    async def fetch(self, client: HttpClient) -> Sequence[RawJob]:
        semaphore = asyncio.Semaphore(BOARD_CONCURRENCY)

        async def one(board: Board) -> list[RawJob]:
            async with semaphore:
                return await self._fetch_board(client, board)

        results = await asyncio.gather(
            *(one(board) for board in self.boards), return_exceptions=True
        )

        jobs: list[RawJob] = []
        for board, result in zip(self.boards, results, strict=True):
            if isinstance(result, BaseException):
                # One dead board must not abort the other 200.
                logger.warning("%s board %s failed: %s", self.name, board.token, result)
                continue
            jobs.extend(result)
        return jobs

    async def _fetch_board(self, client: HttpClient, board: Board) -> list[RawJob]:
        url = self.provider.endpoint(board.token)
        try:
            payload = await client.get_json(url, kind=RequestKind.API)
        except FetchError as error:
            logger.info("%s: board %r unreachable (%s)", self.name, board.token, error)
            return []

        jobs: list[RawJob] = []
        for posting in self.provider.extract(payload):
            try:
                job = self.provider.map_job(posting, board)
            except Exception:
                logger.exception("%s: could not map a posting from %r", self.name, board.token)
                continue
            if job and job.title and job.url:
                jobs.append(job)

        logger.debug("%s: %s -> %d jobs", self.name, board.token, len(jobs))
        return jobs


def build_connectors(boards: Iterable[Board]) -> list[AtsConnector]:
    """Group boards by provider and build one connector per provider."""
    grouped: dict[str, list[Board]] = {}
    for board in boards:
        if board.provider not in PROVIDERS:
            logger.warning("unknown ATS provider %r for board %r", board.provider, board.token)
            continue
        grouped.setdefault(board.provider, []).append(board)
    return [AtsConnector(PROVIDERS[name], group) for name, group in sorted(grouped.items())]
