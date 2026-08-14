"""Finding a way to actually apply to a company.

Nothing here signs in anywhere. It reads public pages the company published,
and for the sites that forbid automation it produces a link for a human.
"""

from jobbot.contacts.assisted import AssistedLink, links_for_company
from jobbot.contacts.emails import (
    FoundEmail,
    best_hiring_email,
    contact_page_urls,
    extract_emails,
    score_address,
)
from jobbot.contacts.pages import (
    CAREER_PATHS,
    CareerLink,
    candidate_urls,
    find_career_links,
    looks_like_careers_page,
)

__all__ = [
    "CAREER_PATHS",
    "AssistedLink",
    "CareerLink",
    "FoundEmail",
    "best_hiring_email",
    "candidate_urls",
    "contact_page_urls",
    "extract_emails",
    "find_career_links",
    "links_for_company",
    "looks_like_careers_page",
    "score_address",
]
