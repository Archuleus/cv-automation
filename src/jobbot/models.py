"""Database schema — the single source of truth shared by all three arms.

The arms never call each other directly. Arm 1 writes companies and jobs, arm 2
reads jobs and writes contacts, arm 3 reads both and writes applications. Every
handoff is a row transition, which means any arm can be re-run in isolation and
a crash mid-batch never loses work.

Two constraints here carry most of the system's safety:

* ``uq_application_company_period`` makes a duplicate application to the same
  company within the same month impossible at the storage layer, not merely
  unlikely at the application layer.
* ``uq_job_url_hash`` makes re-ingesting the same posting from a second source a
  no-op instead of a duplicate.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


def url_fingerprint(url: str) -> str:
    """Stable short hash of a URL, used for cross-source deduplication."""
    normalised = url.strip().lower().rstrip("/")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def period_of(moment: datetime | date | None = None) -> str:
    """Calendar month key (``YYYY-MM``) used by the per-company cooldown."""
    moment = moment or utcnow()
    return f"{moment.year:04d}-{moment.month:02d}"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Where a company or job was discovered."""

    ATS = "ats"  # Greenhouse, Lever, Ashby, ... official board APIs
    JOB_BOARD = "job_board"  # RemoteOK, Arbeitnow, Adzuna, EURES, HN threads
    TR_REGISTRY = "tr_registry"  # Teknopark / OSB / TOBB style company directories
    ASSISTED = "assisted"  # LinkedIn, kariyer.net - URL generated, opened by a human
    MANUAL = "manual"


class ContactKind(StrEnum):
    """How an application can actually be delivered, best channel first."""

    ATS_FORM = "ats_form"
    CAREER_PAGE = "career_page"
    EMAIL = "email"
    LINKEDIN = "linkedin"
    KARIYERNET = "kariyernet"


# Preference order for the arm-2 cascade. Lower index wins.
CONTACT_PRIORITY: tuple[ContactKind, ...] = (
    ContactKind.ATS_FORM,
    ContactKind.CAREER_PAGE,
    ContactKind.EMAIL,
    ContactKind.LINKEDIN,
    ContactKind.KARIYERNET,
)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    REJECTED = "rejected"  # below match threshold
    CONTACT_PENDING = "contact_pending"
    CONTACT_FOUND = "contact_found"
    NO_CONTACT_FOUND = "no_contact_found"
    APPLIED = "applied"
    EXPIRED = "expired"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"
    REPLIED = "replied"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    EDIT_APPROVE = "edit_approve"
    REJECT = "reject"
    BLOCK_COMPANY = "block_company"


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


class Company(SQLModel, table=True):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("domain", name="uq_company_domain"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    domain: str = Field(index=True)
    country: str | None = Field(default=None, index=True)
    size_bucket: str | None = None
    tech_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    source: SourceKind = Field(default=SourceKind.MANUAL, index=True)
    source_ref: str | None = None
    # Set when the user picks "block company" in the review queue. Blocked
    # companies are skipped by every arm, permanently.
    is_blocked: bool = Field(default=False, index=True)
    discovered_at: datetime = Field(default_factory=utcnow)
    # When arm 2 last visited this company's site. Null means never, which is
    # what makes discovery compound: a source can add ten thousand companies and
    # arm 2 works through the ones it has not seen without redoing the rest.
    investigated_at: datetime | None = Field(default=None, index=True)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_job_url_hash"),
        UniqueConstraint("ats_provider", "external_id", name="uq_job_external_id"),
        Index("ix_job_status_score", "status", "match_score"),
    )

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    title: str
    url: str
    url_hash: str = Field(index=True)
    location: str | None = None
    is_remote: bool = False
    seniority: str | None = None
    posted_at: datetime | None = None
    raw_description: str | None = None
    # Present only for ATS-sourced jobs; the pair (provider, external_id) is what
    # makes re-ingestion idempotent even if the public URL changes.
    ats_provider: str | None = Field(default=None, index=True)
    external_id: str | None = None
    match_score: int | None = Field(default=None, index=True)
    match_reason: str | None = None
    status: JobStatus = Field(default=JobStatus.DISCOVERED, index=True)
    discovered_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("company_id", "kind", "value", name="uq_contact_identity"),
    )

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    kind: ContactKind = Field(index=True)
    value: str
    # 0.0-1.0. Guessed addresses are never stored, so anything below ~0.6 means
    # "found on the site but not obviously a hiring channel".
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    discovered_via: str | None = None
    verified_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    __tablename__ = "applications"
    __table_args__ = (
        # The cooldown guard. `company_id` and `period` are denormalised onto
        # this table purely so the database itself can reject a second
        # application to the same company in the same month.
        UniqueConstraint("company_id", "period", name="uq_application_company_period"),
        Index("ix_application_status", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, foreign_key="jobs.id", index=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    contact_id: int | None = Field(default=None, foreign_key="contacts.id")
    period: str = Field(default_factory=period_of, index=True)

    cv_variant: str | None = None
    subject: str | None = None
    body: str | None = None
    status: ApplicationStatus = Field(default=ApplicationStatus.DRAFT, index=True)
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    replied_at: datetime | None = None


class ReviewQueueItem(SQLModel, table=True):
    __tablename__ = "review_queue"
    __table_args__ = (UniqueConstraint("application_id", name="uq_review_application"),)

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="applications.id", index=True)
    priority: int = Field(default=0, index=True)
    note: str | None = None
    decision: ReviewDecision | None = Field(default=None, index=True)
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """Append-only audit trail. Every state change in every arm lands here."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_event_entity", "entity_type", "entity_id"),)

    id: int | None = Field(default=None, primary_key=True)
    entity_type: str = Field(index=True)
    entity_id: int | None = Field(default=None, index=True)
    event: str = Field(index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ts: datetime = Field(default_factory=utcnow, index=True)


ALL_TABLES = (Company, Job, Contact, Application, ReviewQueueItem, Event)
