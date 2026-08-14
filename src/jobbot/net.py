"""Outbound HTTP: politeness first, throughput second.

Everything that leaves this process goes through :class:`HttpClient`, which
enforces three things no connector should have to remember:

* **One request per domain at a time, spaced by a configured interval.** A burst
  of parallel requests against a small company's site is indistinguishable from
  an attack and is the fastest way to get an IP blocked.
* **robots.txt on every HTML fetch.** Requests are declared as either ``API``
  (a documented, public JSON endpoint the vendor publishes for this purpose) or
  ``PAGE`` (fetching someone's website). Only the latter is crawling, and it is
  the one robots.txt governs. Declaring an HTML crawl as an API call to dodge
  the check would defeat the point, so the kind is a required argument.
* **Backoff that honours Retry-After.** Retrying harder against a server that
  just asked for a pause is how a polite client becomes an abusive one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from enum import StrEnum
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from jobbot import __version__
from jobbot.config import Settings, get_settings

logger = logging.getLogger("jobbot.net")

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class RequestKind(StrEnum):
    API = "api"  # documented public JSON endpoint; not crawling
    PAGE = "page"  # someone's website; robots.txt applies


class FetchError(RuntimeError):
    """A request failed permanently, after any retries."""


class RobotsDisallowedError(FetchError):
    """robots.txt forbids fetching this URL."""


def user_agent(settings: Settings | None = None) -> str:
    """Identify the client honestly, with a way to be contacted."""
    settings = settings or get_settings()
    contact = settings.applicant_email or "unspecified"
    return f"jobbot/{__version__} (personal job search automation; contact: {contact})"


class DomainRateLimiter:
    """Serialises requests per host and spaces them by a fixed interval."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._min_interval = 1.0 / requests_per_second
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> Any:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            elapsed_since = loop.time() - self._last_request.get(host, float("-inf"))
            delay = self._min_interval - elapsed_since
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                yield
            finally:
                self._last_request[host] = loop.time()


class RobotsCache:
    """Fetches and caches robots.txt per host."""

    def __init__(self, client: httpx.AsyncClient, agent: str) -> None:
        self._client = client
        self._agent = agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = await self._parser_for(origin)
        if parser is None:
            # No robots.txt, or it could not be read. The convention is that
            # absence means "no restrictions"; the rate limiter still applies.
            return True
        return parser.can_fetch(self._agent, url)

    async def _parser_for(self, origin: str) -> RobotFileParser | None:
        if origin in self._parsers:
            return self._parsers[origin]

        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._parsers:  # filled in while waiting for the lock
                return self._parsers[origin]

            parser: RobotFileParser | None = None
            try:
                response = await self._client.get(f"{origin}/robots.txt", timeout=10.0)
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError as error:
                logger.debug("robots.txt unavailable for %s: %s", origin, error)

            self._parsers[origin] = parser
            return parser


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when present, otherwise exponential backoff."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass  # HTTP-date form; fall through to backoff
    return min(BACKOFF_BASE_SECONDS**attempt, MAX_BACKOFF_SECONDS)


class HttpClient:
    """Rate-limited, robots-aware, retrying HTTP client."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._agent = user_agent(self._settings)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": self._agent, "Accept-Language": "tr,en;q=0.9"},
        )
        self._limiter = DomainRateLimiter(self._settings.domain_rate_limit)
        self._robots = RobotsCache(self._client, self._agent)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        kind: RequestKind,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        attempts: int | None = None,
    ) -> httpx.Response:
        """Fetch a URL.

        `attempts` exists for callers that are *probing*. Retrying an API that
        returned 503 is right; retrying a guessed URL on a domain that refused
        the connection is not, and at four attempts per guess across eight
        guessed paths it turns one dead site into minutes of waiting.
        """
        enforce_robots = kind is RequestKind.PAGE and self._settings.respect_robots
        if enforce_robots and not await self._robots.allows(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")

        host = urlparse(url).netloc
        last_error: Exception | None = None
        max_attempts = max(1, attempts if attempts is not None else MAX_ATTEMPTS)

        for attempt in range(max_attempts):
            async with self._limiter.slot(host):
                try:
                    response = await self._client.get(url, params=params, headers=headers)
                except httpx.HTTPError as error:
                    last_error = error
                    logger.debug("request error %s (attempt %d): %s", url, attempt + 1, error)
                    delay = min(BACKOFF_BASE_SECONDS**attempt, MAX_BACKOFF_SECONDS)
                else:
                    if response.status_code not in RETRYABLE_STATUS:
                        return response
                    last_error = FetchError(f"HTTP {response.status_code} for {url}")
                    delay = _retry_after_seconds(response, attempt)
                    logger.debug(
                        "retryable status %s for %s, waiting %.1fs",
                        response.status_code,
                        url,
                        delay,
                    )

            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)

        raise FetchError(f"giving up on {url} after {max_attempts} attempts") from last_error

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("kind", RequestKind.API)
        response = await self.get(url, **kwargs)
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except ValueError as error:
            raise FetchError(f"response from {url} is not JSON") from error

    async def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs.setdefault("kind", RequestKind.PAGE)
        response = await self.get(url, **kwargs)
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        return response.text


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Split a list into batches, used to bound connector concurrency."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
