"""Search links for the sites this project will not automate.

LinkedIn and kariyer.net both prohibit automated access, and both would ban the
account being used -- which for a job seeker is the account that matters most.
So nothing here fetches anything. It builds the URL a person would have typed,
and a human opens it in their own signed-in browser.

That is not a lesser fallback: for a Turkish employer that publishes only on
kariyer.net, a good search link is the entire contribution this arm can make,
and it costs nothing to be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

LINKEDIN_JOBS = "https://www.linkedin.com/jobs/search/"
LINKEDIN_COMPANY = "https://www.linkedin.com/search/results/companies/"
KARIYERNET_SEARCH = "https://www.kariyer.net/is-ilanlari/"

# LinkedIn's own geo id for Türkiye. Without it the search silently defaults to
# wherever the browser looks like it is.
LINKEDIN_TURKEY_GEO = "102105699"


@dataclass(frozen=True, slots=True)
class AssistedLink:
    """A search a human should open, and why."""

    site: str
    url: str
    label: str

    def as_markdown(self) -> str:
        return f"- [{self.label}]({self.url}) — {self.site}"


def linkedin_company_jobs(company: str) -> AssistedLink:
    """Jobs at one company, which is the highest-yield search there is."""
    query = quote_plus(company)
    return AssistedLink(
        site="LinkedIn",
        url=f"{LINKEDIN_JOBS}?keywords={query}&geoId={LINKEDIN_TURKEY_GEO}&f_TPR=r604800",
        label=f"{company} jobs posted in the last week",
    )


def linkedin_company_page(company: str) -> AssistedLink:
    return AssistedLink(
        site="LinkedIn",
        url=f"{LINKEDIN_COMPANY}?keywords={quote_plus(company)}",
        label=f"{company} company page",
    )


def kariyernet_company(company: str) -> AssistedLink:
    """kariyer.net's own company listing, which is where Turkish SMEs post."""
    return AssistedLink(
        site="kariyer.net",
        url=f"{KARIYERNET_SEARCH}?kw={quote_plus(company)}",
        label=f"{company} on kariyer.net",
    )


def kariyernet_role(role: str, *, city: str = "") -> AssistedLink:
    query = quote_plus(f"{role} {city}".strip())
    return AssistedLink(
        site="kariyer.net",
        url=f"{KARIYERNET_SEARCH}?kw={query}",
        label=f"{role}{f' in {city}' if city else ''} on kariyer.net",
    )


def links_for_company(company: str) -> list[AssistedLink]:
    """Everything worth opening by hand for one employer."""
    return [
        linkedin_company_jobs(company),
        kariyernet_company(company),
        linkedin_company_page(company),
    ]
