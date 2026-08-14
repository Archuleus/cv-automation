"""The rules that decide whether a message may be sent right now.

These are checked against the database, not held in memory, so restarting the
process cannot reset a cap and two processes cannot each send "the last one".

The asymmetry is the point. A message delayed by an hour costs nothing. A
mailbox that gets classified as a bulk sender stops every future application
from arriving, is invisible when it happens, and cannot be undone -- so every
rule here fails closed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, func, select

from jobbot.config import Settings
from jobbot.models import Application, ApplicationStatus, Company


@dataclass(frozen=True, slots=True)
class SendDecision:
    allowed: bool
    reason: str = ""
    wait_seconds: int = 0

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = SendDecision(allowed=True)


def sent_today(session: Session, *, now: datetime | None = None) -> int:
    """How many applications have already gone out in the last 24 hours."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=1)
    # A NULL sent_at fails the >= comparison in SQL, so rows that were never
    # sent are excluded without a separate null check. Writing that check as
    # `Application.sent_at is not None` would be a Python identity test that
    # evaluates to True before SQLAlchemy ever sees it -- a filter that silently
    # matches everything.
    count = session.exec(
        select(func.count())
        .select_from(Application)
        .where(Application.status == ApplicationStatus.SENT)
        .where(col(Application.sent_at) >= since)
    ).one()
    return int(count)


def last_send_at(session: Session) -> datetime | None:
    """When the most recent application went out."""
    return session.exec(
        select(Application.sent_at)
        .where(Application.status == ApplicationStatus.SENT)
        .order_by(col(Application.sent_at).desc())
        .limit(1)
    ).first()


def next_gap_seconds(settings: Settings, *, rng: random.Random | None = None) -> int:
    """A randomised pause between sends.

    Fixed intervals are a stronger bulk-sender signal than volume is: nothing
    human sends mail exactly every 90 seconds.
    """
    generator = rng or random.SystemRandom()
    return generator.randint(settings.send_min_gap_seconds, settings.send_max_gap_seconds)


def may_send(
    session: Session,
    *,
    settings: Settings,
    company_id: int,
    now: datetime | None = None,
) -> SendDecision:
    """Every gate a message must pass, most permanent first."""
    now = now or datetime.now(UTC)

    company = session.get(Company, company_id)
    if company is None:
        return SendDecision(False, f"company {company_id} does not exist")
    if company.is_blocked:
        return SendDecision(False, f"{company.name} is blocked")

    # The per-company cooldown also exists as a database constraint; this check
    # produces a readable reason instead of an IntegrityError at insert time.
    cutoff = now - timedelta(days=settings.company_cooldown_days)
    recent = session.exec(
        select(Application)
        .where(Application.company_id == company_id)
        .where(Application.status == ApplicationStatus.SENT)
        .where(col(Application.sent_at) >= cutoff)
        .limit(1)
    ).first()
    if recent is not None:
        return SendDecision(
            False,
            f"already applied to {company.name} within "
            f"{settings.company_cooldown_days} days",
        )

    today = sent_today(session, now=now)
    if today >= settings.max_applications_per_day:
        return SendDecision(
            False, f"daily cap reached ({today}/{settings.max_applications_per_day})"
        )

    previous = last_send_at(session)
    if previous is not None:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        elapsed = (now - previous).total_seconds()
        if elapsed < settings.send_min_gap_seconds:
            wait = int(settings.send_min_gap_seconds - elapsed)
            return SendDecision(False, f"last send was {int(elapsed)}s ago", wait_seconds=wait)

    return ALLOWED
