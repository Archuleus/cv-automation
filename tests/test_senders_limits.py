"""The gates a message must pass before it may be sent.

These matter more than the send itself: a delayed application costs nothing,
while a mailbox filtered as a bulk sender silently stops every future one.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from jobbot.config import Settings
from jobbot.models import Application, ApplicationStatus, Company
from jobbot.senders.limits import last_send_at, may_send, next_gap_seconds, sent_today

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        max_applications_per_day=3,
        company_cooldown_days=30,
        send_min_gap_seconds=90,
        send_max_gap_seconds=420,
    )


@pytest.fixture
def company(session: Session) -> Company:
    record = Company(name="Acme", domain="acme.com.tr")
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def _sent(session: Session, company_id: int, *, when: datetime, period: str) -> Application:
    application = Application(
        company_id=company_id,
        period=period,
        status=ApplicationStatus.SENT,
        sent_at=when,
    )
    session.add(application)
    session.commit()
    return application


class TestSentToday:
    def test_counts_nothing_in_an_empty_database(self, session: Session):
        assert sent_today(session, now=NOW) == 0

    def test_counts_only_sent_applications(self, session: Session, company: Company):
        session.add(Application(company_id=company.id, period="2026-08"))  # draft
        session.commit()

        assert sent_today(session, now=NOW) == 0

    def test_counts_sends_inside_the_window(self, session: Session, company: Company):
        _sent(session, company.id, when=NOW - timedelta(hours=3), period="2026-08")

        assert sent_today(session, now=NOW) == 1

    def test_ignores_sends_older_than_a_day(self, session: Session, company: Company):
        _sent(session, company.id, when=NOW - timedelta(days=2), period="2026-06")

        assert sent_today(session, now=NOW) == 0

    def test_a_never_sent_row_does_not_count(self, session: Session, company: Company):
        # `sent_at IS NULL` must not slip through the window filter. Writing that
        # filter as a Python `is not None` test would match every row.
        session.add(
            Application(company_id=company.id, period="2026-08", status=ApplicationStatus.SENT)
        )
        session.commit()

        assert sent_today(session, now=NOW) == 0


class TestLastSendAt:
    def test_none_when_nothing_was_sent(self, session: Session):
        assert last_send_at(session) is None

    def test_returns_the_most_recent(self, session: Session, company: Company):
        _sent(session, company.id, when=NOW - timedelta(days=40), period="2026-07")
        _sent(session, company.id, when=NOW - timedelta(hours=2), period="2026-08")

        result = last_send_at(session)

        assert result is not None
        assert result.replace(tzinfo=UTC) == NOW - timedelta(hours=2)


class TestMaySend:
    def test_allows_a_first_application(self, session: Session, company: Company, settings):
        assert may_send(session, settings=settings, company_id=company.id, now=NOW)

    def test_refuses_an_unknown_company(self, session: Session, settings):
        decision = may_send(session, settings=settings, company_id=9999, now=NOW)

        assert not decision
        assert "does not exist" in decision.reason

    def test_refuses_a_blocked_company(self, session: Session, company: Company, settings):
        company.is_blocked = True
        session.add(company)
        session.commit()

        decision = may_send(session, settings=settings, company_id=company.id, now=NOW)

        assert not decision
        assert "blocked" in decision.reason

    def test_refuses_inside_the_company_cooldown(
        self, session: Session, company: Company, settings
    ):
        _sent(session, company.id, when=NOW - timedelta(days=10), period="2026-08")

        decision = may_send(session, settings=settings, company_id=company.id, now=NOW)

        assert not decision
        assert "within 30 days" in decision.reason

    def test_allows_again_after_the_cooldown(self, session: Session, company: Company, settings):
        _sent(session, company.id, when=NOW - timedelta(days=45), period="2026-06")

        assert may_send(session, settings=settings, company_id=company.id, now=NOW)

    def test_refuses_once_the_daily_cap_is_reached(self, session: Session, settings):
        for index in range(3):
            other = Company(name=f"C{index}", domain=f"c{index}.com")
            session.add(other)
            session.commit()
            _sent(session, other.id, when=NOW - timedelta(hours=index + 1), period="2026-08")
        target = Company(name="Target", domain="target.com")
        session.add(target)
        session.commit()

        decision = may_send(session, settings=settings, company_id=target.id, now=NOW)

        assert not decision
        assert "daily cap reached (3/3)" in decision.reason

    def test_refuses_when_the_previous_send_was_too_recent(self, session: Session, settings):
        first = Company(name="First", domain="first.com")
        second = Company(name="Second", domain="second.com")
        session.add_all([first, second])
        session.commit()
        _sent(session, first.id, when=NOW - timedelta(seconds=30), period="2026-08")

        decision = may_send(session, settings=settings, company_id=second.id, now=NOW)

        assert not decision
        assert decision.wait_seconds == pytest.approx(60, abs=2)

    def test_allows_once_the_gap_has_elapsed(self, session: Session, settings):
        first = Company(name="First", domain="first.com")
        second = Company(name="Second", domain="second.com")
        session.add_all([first, second])
        session.commit()
        _sent(session, first.id, when=NOW - timedelta(seconds=200), period="2026-08")

        assert may_send(session, settings=settings, company_id=second.id, now=NOW)

    def test_a_blocked_company_beats_an_available_quota(
        self, session: Session, company: Company, settings
    ):
        # Ordering matters: the most permanent reason should be the one reported.
        company.is_blocked = True
        session.add(company)
        session.commit()

        assert "blocked" in may_send(
            session, settings=settings, company_id=company.id, now=NOW
        ).reason


class TestPacing:
    def test_gap_falls_inside_the_configured_range(self, settings):
        rng = random.Random(0)

        gaps = {next_gap_seconds(settings, rng=rng) for _ in range(50)}

        assert all(90 <= gap <= 420 for gap in gaps)

    def test_gap_is_not_constant(self, settings):
        # A fixed interval is a stronger bulk-sender signal than volume is.
        rng = random.Random(0)

        gaps = {next_gap_seconds(settings, rng=rng) for _ in range(30)}

        assert len(gaps) > 1


class TestSettingsValidation:
    def test_rejects_an_inverted_gap_range(self):
        with pytest.raises(ValueError, match="send_max_gap_seconds"):
            Settings(_env_file=None, send_min_gap_seconds=300, send_max_gap_seconds=60)
