"""Finding Turkish software companies through their GitHub organisations.

An organisation account with a website is a strong signal that a company
employs engineers, and it is a signal that scales down: the two-person startup
in Eskişehir has one for the same reason Trendyol does. That is the opposite of
the bias in any hand-written list, which only ever contains companies famous
enough to be remembered.

The GitHub REST API is public and documented, so this is an API call rather than
crawling. Unauthenticated search is limited to ten requests a minute, which is
fine for a background job; setting GITHUB_TOKEN raises it and is worth doing
before a large run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from jobbot.discovery.base import DiscoveredCompany
from jobbot.models import SourceKind
from jobbot.net import FetchError, HttpClient, RequestKind

logger = logging.getLogger("jobbot.discovery.github")

SEARCH_URL = "https://api.github.com/search/users"
USER_URL = "https://api.github.com/users"

# Searched separately: GitHub's location field is free text, so "Istanbul" and
# "Türkiye" return substantially different sets and neither is a superset.
TURKISH_LOCATIONS: tuple[str, ...] = (
    "Turkey",
    "Türkiye",
    "Istanbul",
    "İstanbul",
    "Ankara",
    "Izmir",
    "İzmir",
    "Bursa",
    "Antalya",
    "Kocaeli",
    "Eskisehir",
    "Konya",
    "Kayseri",
    "Gaziantep",
    "Denizli",
    "Adana",
    "Trabzon",
    "Samsun",
)

PER_PAGE = 100
# GitHub caps search results at 1000 per query regardless of paging.
MAX_PAGES = 4


class GitHubOrgSource:
    """Organisations whose profile places them in Türkiye."""

    name = "github-orgs"

    def __init__(
        self,
        *,
        locations: Sequence[str] = TURKISH_LOCATIONS,
        token: str = "",
        max_pages: int = MAX_PAGES,
        max_profiles: int = 400,
    ) -> None:
        self.locations = tuple(locations)
        self._token = token
        self._max_pages = max_pages
        self._max_profiles = max_profiles

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _logins(self, client: HttpClient, location: str) -> list[str]:
        logins: list[str] = []
        for page in range(1, self._max_pages + 1):
            try:
                payload = await client.get_json(
                    SEARCH_URL,
                    kind=RequestKind.API,
                    headers=self._headers(),
                    params={
                        "q": f'type:org location:"{location}"',
                        "per_page": PER_PAGE,
                        "page": page,
                    },
                )
            except FetchError as error:
                # Search is the most rate-limited endpoint; stopping this
                # location is better than abandoning the whole run.
                logger.info("github search for %r stopped: %s", location, error)
                break

            items = payload.get("items", []) if isinstance(payload, dict) else []
            logins.extend(str(item["login"]) for item in items if item.get("login"))
            if len(items) < PER_PAGE:
                break
        return logins

    async def _profile(self, client: HttpClient, login: str) -> DiscoveredCompany | None:
        try:
            payload = await client.get_json(
                f"{USER_URL}/{login}", kind=RequestKind.API, headers=self._headers()
            )
        except FetchError:
            return None

        website = str(payload.get("blog") or "").strip()
        if not website:
            # No site means no careers page to visit, so nothing arm 2 can use.
            return None

        return DiscoveredCompany(
            name=str(payload.get("name") or login),
            domain=website,
            source=SourceKind.TR_REGISTRY,
            source_ref=f"github:{login}",
        )

    async def discover(self, client: HttpClient) -> list[DiscoveredCompany]:
        seen: set[str] = set()
        for location in self.locations:
            for login in await self._logins(client, location):
                if login.lower() not in seen:
                    seen.add(login.lower())
            if len(seen) >= self._max_profiles:
                break

        logger.info("github: %d organisations found, reading profiles", len(seen))
        companies: list[DiscoveredCompany] = []
        for login in list(seen)[: self._max_profiles]:
            company = await self._profile(client, login)
            if company is not None and company.is_usable:
                companies.append(company)

        logger.info("github: %d organisations have a usable website", len(companies))
        return companies
