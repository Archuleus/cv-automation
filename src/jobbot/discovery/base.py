"""Where new companies come from.

A hand-written list of employers goes stale the day it is written and is biased
towards whoever is famous — which is exactly the wrong bias for a new graduate,
because the well-known names get thousands of applications and rarely hire
juniors. The companies worth finding are the ones nobody has heard of.

So discovery is a source, not a file. Each source answers one question — "which
companies exist that we do not know about" — and arm 2 works through whatever
they find. Adding a source grows the search permanently; editing a JSON file
grows it once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlparse

from jobbot.models import SourceKind

if TYPE_CHECKING:
    from jobbot.net import HttpClient

# Hosts that appear in company directories but are never the company.
NON_COMPANY_HOSTS: frozenset[str] = frozenset((
    # social and content platforms every directory links to
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "github.com", "gitlab.com", "medium.com", "wordpress.com",
    "blogspot.com", "whatsapp.com", "telegram.org", "t.co", "bit.ly",
    # platforms and reference sites
    "google.com", "goo.gl", "maps.google.com", "apple.com", "play.google.com",
    "wikipedia.org",
    # job boards: these are where companies post, not companies
    "kariyer.net", "secretcv.com", "yenibiris.com", "eleman.net",
    "indeed.com", "glassdoor.com",
    # the ministries and agencies that fund technoparks
    "gov.tr", "tubitak.gov.tr", "kosgeb.gov.tr", "sanayi.gov.tr",
))


def normalise_domain(raw: str) -> str:
    """Reduce a URL or hostname to a bare registrable domain."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "//" not in value:
        value = f"//{value}"
    host = urlparse(value).netloc or urlparse(value).path
    host = host.split("/")[0].split("@")[-1].split(":")[0]
    return host.removeprefix("www.").strip(". ")


def is_company_domain(domain: str) -> bool:
    """Filter out the social, government and job-board links in any directory."""
    if not domain or "." not in domain or " " in domain:
        return False
    if any(domain == host or domain.endswith(f".{host}") for host in NON_COMPANY_HOSTS):
        return False
    # A bare TLD or a single label is never a company site.
    return len(domain.split(".")) >= 2 and len(domain) > 4


@dataclass(frozen=True, slots=True)
class DiscoveredCompany:
    name: str
    domain: str
    source: SourceKind = SourceKind.TR_REGISTRY
    source_ref: str = ""
    country: str = "TR"

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", normalise_domain(self.domain))

    @property
    def is_usable(self) -> bool:
        return bool(self.name.strip()) and is_company_domain(self.domain)


@runtime_checkable
class CompanySource(Protocol):
    """Something that can name companies we do not already know about."""

    name: str

    async def discover(self, client: HttpClient) -> Sequence[DiscoveredCompany]: ...


def deduplicate(companies: Sequence[DiscoveredCompany]) -> list[DiscoveredCompany]:
    """One entry per domain, keeping the first name seen for it."""
    seen: dict[str, DiscoveredCompany] = {}
    for company in companies:
        if company.is_usable and company.domain not in seen:
            seen[company.domain] = company
    return list(seen.values())
