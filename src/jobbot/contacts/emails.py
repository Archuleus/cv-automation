"""Collecting a company's published hiring address.

Only addresses the company itself put on its own pages are kept. Nothing is
guessed -- not `ik@`, not `careers@`, not any of the patterns that would
obviously "probably work". A guessed address that bounces teaches the receiving
mail system that this sender does not know who it is writing to, and enough of
those quietly move every later application to spam. The rule is worth more than
the extra contacts it costs.

Ranking matters as much as extraction: `ik@` reaches a recruiter, `info@`
reaches a shared inbox nobody owns, and a personal address reaches someone who
did not ask to be written to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from jobbot.normalize import fold

# Deliberately conservative: no unicode locals, no trailing dots. A missed
# address costs one contact; a malformed one costs a bounce.
EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
)

# Local parts that reach someone whose job is hiring, best first.
HIRING_LOCALS: tuple[str, ...] = (
    "ik",
    "insankaynaklari",
    "insan.kaynaklari",
    "kariyer",
    "isbasvuru",
    "basvuru",
    "hr",
    "careers",
    "career",
    "jobs",
    "job",
    "recruiting",
    "recruitment",
    "talent",
    "hiring",
    "cv",
)

# Shared inboxes: real, but nobody owns them.
GENERIC_LOCALS: tuple[str, ...] = (
    "info",
    "iletisim",
    "contact",
    "hello",
    "merhaba",
    "destek",
    "support",
    "bilgi",
)

# Addresses that exist on a page but are never a place to send an application.
UNWANTED_LOCALS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "donotreply",
    "postmaster",
    "abuse",
    "webmaster",
    "hostmaster",
    "privacy",
    "kvkk",
    "gdpr",
    "legal",
    "press",
    "basin",
    "invoice",
    "fatura",
    "billing",
    "sales",
    "satis",
    "bayi",
    "spam",
    "example",
    "sentry",
    "wordpress",
)

# File extensions that the pattern happily matches inside asset filenames.
ASSET_SUFFIXES: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico",
    ".woff", ".woff2", ".ttf", ".mp4", ".pdf", ".webm",
)

HIRING_SCORE = 0.9
GENERIC_SCORE = 0.5
OFFSITE_PENALTY = 0.2


@dataclass(frozen=True, slots=True)
class FoundEmail:
    address: str
    confidence: float
    source_url: str = ""

    @property
    def local(self) -> str:
        return self.address.split("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.address.split("@", 1)[1]


def _is_plausible(address: str) -> bool:
    local, _, domain = address.partition("@")
    if not local or not domain or ".." in address:
        return False
    if any(domain.endswith(suffix) for suffix in ASSET_SUFFIXES):
        return False
    if any(fold(local).startswith(word) for word in UNWANTED_LOCALS):
        return False
    # A local part of pure hex is almost always a tracking or asset hash.
    return not (len(local) > 24 and re.fullmatch(r"[0-9a-f]+", local.lower()))


def score_address(address: str, *, company_domain: str) -> float:
    """How likely is this to reach someone who reads applications?"""
    local = fold(address.split("@", 1)[0])
    domain = address.split("@", 1)[1].lower()

    if any(local == word or local.startswith(f"{word}.") for word in HIRING_LOCALS):
        score = HIRING_SCORE
    elif any(word in local for word in HIRING_LOCALS):
        score = HIRING_SCORE - 0.15
    elif local in GENERIC_LOCALS:
        score = GENERIC_SCORE
    else:
        # A named individual. Real, but they did not ask to be written to.
        score = 0.3

    root = company_domain.lower().removeprefix("www.")
    if not (domain == root or domain.endswith(f".{root}")):
        score -= OFFSITE_PENALTY
    return round(max(0.0, min(1.0, score)), 2)


def extract_emails(html: str, *, company_domain: str, source_url: str = "") -> list[FoundEmail]:
    """Every usable address published on a page, best first.

    `mailto:` links are trusted more than loose text because they are markup the
    site author wrote deliberately, not a string that happened to look like an
    address.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    candidates: dict[str, float] = {}

    def offer(raw: str, bonus: float) -> None:
        address = raw.strip().strip(".,;:()<>[]\"'").lower()
        if not EMAIL_PATTERN.fullmatch(address) or not _is_plausible(address):
            return
        score = min(1.0, score_address(address, company_domain=company_domain) + bonus)
        if score > candidates.get(address, 0.0):
            candidates[address] = score

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if href.lower().startswith("mailto:"):
            offer(unquote(href[7:].split("?", 1)[0]), 0.05)

    for match in EMAIL_PATTERN.finditer(soup.get_text(" ", strip=True)):
        offer(match.group(0), 0.0)

    return sorted(
        (
            FoundEmail(address=address, confidence=score, source_url=source_url)
            for address, score in candidates.items()
        ),
        key=lambda found: (-found.confidence, found.address),
    )


def contact_page_urls(domain: str) -> list[str]:
    """Pages worth checking when the careers page carried no address."""
    root = domain.strip().lower().removeprefix("www.").rstrip("/")
    paths = (
        "/iletisim",
        "/contact",
        "/contact-us",
        "/bize-ulasin",
        "/hakkimizda",
        "/about",
        "/about-us",
    )
    return [f"https://{root}{path}" for path in paths]


def best_hiring_email(found: list[FoundEmail]) -> FoundEmail | None:
    """The single address most likely to reach a person who hires."""
    usable = [item for item in found if item.confidence >= GENERIC_SCORE]
    return usable[0] if usable else None


def same_site(address: str, domain: str) -> bool:
    host = urlparse(f"//{address.split('@', 1)[1]}").path or address.split("@", 1)[1]
    root = domain.lower().removeprefix("www.")
    return host == root or host.endswith(f".{root}")
