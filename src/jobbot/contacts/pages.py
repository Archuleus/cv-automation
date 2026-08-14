"""Finding a company's careers page.

Two strategies, cheapest first. Guessing well-known paths costs one request per
guess and succeeds surprisingly often, because `/kariyer` and `/careers` are
near-universal. Reading the homepage costs one request and finds the rest,
including the sites that put hiring under `/hakkimizda/bize-katilin` or behind a
subdomain.

Turkish sites need their own path list and their own link vocabulary; a crawler
that only knows the word "careers" misses most of the Turkish market, which is
the entire reason this arm exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from jobbot.normalize import fold

logger = logging.getLogger("jobbot.contacts.pages")

# Tried in order against the company's own domain.
CAREER_PATHS: tuple[str, ...] = (
    # Turkish
    "/kariyer",
    "/kariyer-firsatlari",
    "/insan-kaynaklari",
    "/ik",
    "/bize-katil",
    "/bize-katilin",
    "/is-basvurusu",
    "/acik-pozisyonlar",
    "/kariyer/acik-pozisyonlar",
    "/tr/kariyer",
    # English
    "/careers",
    "/career",
    "/jobs",
    "/join-us",
    "/work-with-us",
    "/en/careers",
    "/about/careers",
    "/company/careers",
    "/opportunities",
    "/vacancies",
)

# Link text that means "we are hiring". Folded before comparison, so Turkish
# diacritics do not have to be repeated here.
CAREER_LINK_WORDS: tuple[str, ...] = (
    "kariyer",
    "insan kaynaklari",
    "bize katil",
    "is basvurusu",
    "acik pozisyon",
    "ekibimize katil",
    "is ilanlari",
    "careers",
    "career",
    "jobs",
    "join us",
    "join our team",
    "work with us",
    "we are hiring",
    "open positions",
    "vacancies",
    "opportunities",
)

# Words that appear in career-adjacent links that are not the careers page.
NEGATIVE_LINK_WORDS: tuple[str, ...] = (
    "kariyer.net",
    "blog",
    "haber",
    "news",
    "press",
    "basin",
)


@dataclass(frozen=True, slots=True)
class CareerLink:
    url: str
    label: str
    confidence: float

    @property
    def is_offsite(self) -> bool:
        return self.confidence < 1.0


def _same_site(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    root = domain.lower().removeprefix("www.")
    return host == root or host.endswith(f".{root}")


def candidate_urls(domain: str) -> list[str]:
    """The well-known paths worth trying on this domain, https first."""
    root = domain.strip().lower().removeprefix("www.").rstrip("/")
    return [f"https://{root}{path}" for path in CAREER_PATHS]


def looks_like_careers_page(html: str) -> bool:
    """Does this page actually describe jobs, or is it a soft 404?

    Many sites answer every path with 200 and their homepage, so the status code
    proves nothing. Requiring hiring vocabulary in the visible text is what
    separates a real careers page from a redirect that pretended to work.
    """
    if not html:
        return False
    text = fold(BeautifulSoup(html, "lxml").get_text(" ", strip=True))[:20_000]
    hits = sum(1 for word in CAREER_LINK_WORDS if fold(word) in text)
    strong = any(
        fold(word) in text
        for word in ("acik pozisyon", "open position", "is ilanlari", "basvur", "apply")
    )
    return hits >= 2 or strong


def is_careers_hub(html: str, *, url: str, domain: str) -> bool:
    """Is this page a careers page, including when its text is rendered by JS?

    Large enterprise sites frequently serve a shell whose only visible text is
    "please enable scripts and reload this page" -- Turkcell's HR page is
    exactly this. The text heuristic correctly finds no hiring vocabulary, but
    the page's links are in the HTML and point at the real careers sections.

    Only applied to URLs that were already guessed as careers paths, so the
    address itself has established intent and a stray "Kariyer" link in a
    footer cannot promote an unrelated page.
    """
    if looks_like_careers_page(html):
        return True
    return len(find_career_links(html, base_url=url, domain=domain)) >= 2


def find_career_links(html: str, *, base_url: str, domain: str) -> list[CareerLink]:
    """Career links on a homepage, best first.

    An off-site link is kept but scored lower: a company that sends its careers
    link straight to an ATS has given us the most useful destination there is,
    while one that links to a job board has given us the least.
    """
    if not html:
        return []

    found: dict[str, CareerLink] = {}
    for anchor in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        label = fold(anchor.get_text(" ", strip=True))[:80]
        target = urljoin(base_url, href)
        haystack = f"{label} {fold(target)}"

        if any(fold(word) in haystack for word in NEGATIVE_LINK_WORDS):
            continue
        if not any(fold(word) in haystack for word in CAREER_LINK_WORDS):
            continue

        # On-site links are the company's own careers page; off-site ones are
        # usually an ATS, which is better still but needs separate handling.
        confidence = 1.0 if _same_site(target, domain) else 0.8
        if label and any(fold(word) == label for word in CAREER_LINK_WORDS):
            confidence = min(1.0, confidence + 0.1)

        existing = found.get(target)
        if existing is None or existing.confidence < confidence:
            found[target] = CareerLink(url=target, label=label, confidence=confidence)

    return sorted(found.values(), key=lambda link: -link.confidence)
