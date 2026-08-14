"""Schema-level guarantees.

These are the constraints the rest of the system trusts blindly, so they are
tested against a real database rather than asserted in application code.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from jobbot.models import (
    CONTACT_PRIORITY,
    Application,
    Company,
    Contact,
    ContactKind,
    Job,
    period_of,
    url_fingerprint,
)


def _job(company: Company, url: str = "https://acme.com.tr/jobs/1", **overrides: object) -> Job:
    payload: dict[str, object] = {
        "company_id": company.id,
        "title": "Backend Developer",
        "url": url,
        "url_hash": url_fingerprint(url),
    }
    payload.update(overrides)
    return Job(**payload)  # type: ignore[arg-type]


class TestUrlFingerprint:
    def test_ignores_trailing_slash_and_case(self):
        # Arrange
        variants = [
            "https://Acme.com.tr/Jobs/1",
            "https://acme.com.tr/jobs/1/",
            "  https://acme.com.tr/jobs/1  ",
        ]

        # Act
        hashes = {url_fingerprint(url) for url in variants}

        # Assert
        assert len(hashes) == 1

    def test_distinguishes_different_urls(self):
        assert url_fingerprint("https://a.com/1") != url_fingerprint("https://a.com/2")


class TestPeriodOf:
    def test_formats_as_year_month(self):
        assert period_of(date(2026, 3, 9)) == "2026-03"

    def test_pads_single_digit_month(self):
        assert period_of(datetime(2026, 1, 1)) == "2026-01"


class TestCompanyConstraints:
    def test_rejects_duplicate_domain(self, session: Session, company: Company):
        # Arrange
        duplicate = Company(name="Acme Yazilim A.S.", domain=company.domain)

        # Act / Assert
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()


class TestJobConstraints:
    def test_rejects_same_url_from_a_second_source(self, session: Session, company: Company):
        # Arrange
        session.add(_job(company))
        session.commit()

        # Act / Assert -- same posting, different discovery run
        session.add(_job(company))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_rejects_same_ats_external_id(self, session: Session, company: Company):
        # Arrange -- an ATS may expose the same posting under two URLs
        session.add(
            _job(company, url="https://a.com/1", ats_provider="greenhouse", external_id="7")
        )
        session.commit()

        # Act / Assert
        session.add(
            _job(company, url="https://a.com/2", ats_provider="greenhouse", external_id="7")
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_allows_same_external_id_across_providers(self, session: Session, company: Company):
        # Arrange
        session.add(
            _job(company, url="https://a.com/1", ats_provider="greenhouse", external_id="7")
        )
        session.commit()

        # Act
        session.add(_job(company, url="https://a.com/2", ats_provider="lever", external_id="7"))
        session.commit()

        # Assert
        assert session.get(Job, 2) is not None

    def test_rejects_job_for_unknown_company(self, session: Session):
        # Foreign keys are off by default in SQLite; this proves the pragma is on.
        session.add(Job(company_id=9999, title="X", url="https://x.com", url_hash="abc"))
        with pytest.raises(IntegrityError):
            session.commit()


class TestContactConstraints:
    def test_rejects_exact_duplicate_contact(self, session: Session, company: Company):
        # Arrange
        for _ in range(2):
            session.add(
                Contact(company_id=company.id, kind=ContactKind.EMAIL, value="ik@acme.com.tr")
            )
        # Act / Assert
        with pytest.raises(IntegrityError):
            session.commit()

    def test_allows_multiple_channels_per_company(self, session: Session, company: Company):
        # Arrange / Act
        session.add(Contact(company_id=company.id, kind=ContactKind.EMAIL, value="ik@acme.com.tr"))
        session.add(
            Contact(company_id=company.id, kind=ContactKind.CAREER_PAGE, value="https://a/careers")
        )
        session.commit()

        # Assert
        kinds = {contact.kind for contact in session.exec(select(Contact)).all()}
        assert kinds == {ContactKind.EMAIL, ContactKind.CAREER_PAGE}

    def test_priority_order_prefers_direct_channels(self):
        assert CONTACT_PRIORITY[0] is ContactKind.ATS_FORM
        assert CONTACT_PRIORITY.index(ContactKind.EMAIL) < CONTACT_PRIORITY.index(
            ContactKind.LINKEDIN
        )
        assert set(CONTACT_PRIORITY) == set(ContactKind)


class TestApplicationCooldown:
    def test_rejects_second_application_to_same_company_in_same_month(
        self, session: Session, company: Company
    ):
        # Arrange -- two different openings at one company, same month
        session.add(Application(company_id=company.id, period="2026-08"))
        session.commit()

        # Act / Assert
        session.add(Application(company_id=company.id, period="2026-08"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_allows_reapplying_in_a_later_month(self, session: Session, company: Company):
        # Arrange
        session.add(Application(company_id=company.id, period="2026-08"))
        session.commit()

        # Act
        session.add(Application(company_id=company.id, period="2026-09"))
        session.commit()

        # Assert
        assert session.get(Application, 2) is not None

    def test_defaults_period_to_current_month(self, session: Session, company: Company):
        # Act
        application = Application(company_id=company.id)
        session.add(application)
        session.commit()

        # Assert
        assert application.period == period_of()
