"""Harvesting company directories: technoparks, incubators, associations.

Türkiye has roughly ninety technoparks and each publishes its member list. That
list is precisely the population a hand-written seed misses -- the twenty-person
company in Gebze that hires two juniors a year and receives forty applications
instead of four thousand.

Every such directory has the same shape: a page of outbound links to member
companies. So rather than writing a parser per site, this extracts links
generically and filters hard. A directory that yields nothing costs one request;
a parser tuned to one site's markup breaks the week they redesign it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from jobbot.config import get_settings
from jobbot.discovery.base import DiscoveredCompany, is_company_domain, normalise_domain
from jobbot.models import SourceKind
from jobbot.net import FetchError, HttpClient, RequestKind

logger = logging.getLogger("jobbot.discovery.directories")

# Link text that is navigation rather than a member company.
NAVIGATION_WORDS: frozenset[str] = frozenset((
    # Turkish
    "ana sayfa", "anasayfa", "hakkımızda", "hakkimizda", "iletişim", "iletisim",
    "haberler", "duyurular", "etkinlikler", "galeri", "basın", "basin",
    "mevzuat", "kvkk", "gizlilik", "çerez", "cerez", "sss", "yardım", "yardim",
    "giriş", "giris", "kayıt", "kayit", "devamı", "devami", "daha fazla",
    # English
    "home", "about", "about us", "contact", "contact us", "news", "events",
    "gallery", "press", "privacy", "cookie", "login", "register", "read more",
    "next", "previous",
))

MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 80


@dataclass(frozen=True, slots=True)
class Directory:
    """One page that lists member companies."""

    name: str
    url: str
    note: str = ""


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())[:MAX_NAME_LENGTH]
    return name.strip(" -–—|·•\t")


def extract_companies(
    html: str,
    *,
    base_url: str,
    directory_name: str,
) -> list[DiscoveredCompany]:
    """Every outbound link on a directory page that looks like a company.

    The filter is deliberately strict. A directory page links to its own
    navigation, to social media, to the ministry that funds it and to the
    universities it sits inside; only links leaving to a distinct domain with a
    plausible name survive.
    """
    if not html:
        return []

    host = normalise_domain(base_url)
    found: dict[str, DiscoveredCompany] = {}

    for anchor in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        target = urljoin(base_url, href)
        if urlparse(target).scheme not in ("http", "https"):
            continue

        domain = normalise_domain(target)
        # An on-site link is the directory's own navigation, not a member.
        if not domain or domain == host or domain.endswith(f".{host}"):
            continue
        if not is_company_domain(domain):
            continue

        label = _clean_name(anchor.get_text(" ", strip=True))
        folded = label.casefold()
        if folded in NAVIGATION_WORDS or len(label) < MIN_NAME_LENGTH:
            # Fall back to the domain when the link is an image or a bare URL.
            label = domain.split(".")[0].replace("-", " ").title()

        if domain not in found:
            found[domain] = DiscoveredCompany(
                name=label,
                domain=domain,
                source=SourceKind.TR_REGISTRY,
                source_ref=f"directory:{directory_name}",
            )

    return list(found.values())


def load_directories(path: Path | None = None) -> tuple[Directory, ...]:
    path = path or get_settings().directories_file
    if not path.exists():
        logger.warning("no directory list at %s", path)
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("directories", payload) if isinstance(payload, dict) else payload
    return tuple(
        Directory(
            name=str(entry["name"]),
            url=str(entry["url"]),
            note=str(entry.get("note", "")),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") and entry.get("url")
    )


class DirectorySource:
    """Member companies listed by technoparks and similar bodies."""

    name = "directories"

    def __init__(self, directories: tuple[Directory, ...] | None = None) -> None:
        self.directories = directories if directories is not None else load_directories()

    async def discover(self, client: HttpClient) -> list[DiscoveredCompany]:
        companies: list[DiscoveredCompany] = []
        for directory in self.directories:
            try:
                html = await client.get_text(
                    directory.url, kind=RequestKind.PAGE, attempts=1
                )
            except (FetchError, Exception) as error:
                # A directory that moved or blocks us costs one request.
                logger.info("directory %s unreachable: %s", directory.name, error)
                continue

            found = extract_companies(
                html, base_url=directory.url, directory_name=directory.name
            )
            logger.info("directory %s: %d companies", directory.name, len(found))
            companies.extend(found)

        return companies
