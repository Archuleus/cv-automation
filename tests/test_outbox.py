"""Preparing applications for a human to send.

Nothing in this path transmits anything, so the guarantees under test are about
what lands on disk and what the database records afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from jobbot.config import Settings
from jobbot.llm.base import Draft
from jobbot.models import (
    Application,
    ApplicationStatus,
    Company,
    Contact,
    ContactKind,
    Job,
    JobStatus,
    url_fingerprint,
)
from jobbot.outbox import build, mark_sent, slugify
from jobbot.outbox.render import Card, render_card, render_index
from tests.test_llm_compose import GOOD, ScriptedClient
from tests.test_llm_validate import POSTING


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite://", company_cooldown_days=30)


def _job(session: Session, name: str, *, score: int = 70) -> tuple[Job, Company]:
    company = Company(name=name, domain=f"{name.lower()}.com")
    session.add(company)
    session.commit()
    url = f"https://boards.example.com/{name.lower()}/jobs/1"
    job = Job(
        company_id=company.id,
        title="Backend Developer",
        url=url,
        url_hash=url_fingerprint(url),
        location="Remote",
        raw_description=POSTING,
        match_score=score,
        match_reason="role family 'backend'",
        status=JobStatus.CONTACT_PENDING,
    )
    session.add(job)
    session.commit()
    return job, company


def _draft() -> Draft:
    return Draft(
        subject=GOOD["subject"],
        body=GOOD["body"],
        cited_detail=GOOD["cited_detail"],
        language="en",
    )


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Canonical (Ubuntu)", "canonical-ubuntu"),
            ("Yazılım Mühendisi", "yazilim-muhendisi"),
            ("C# / .NET Developer", "c-net-developer"),
        ],
    )
    def test_produces_a_filesystem_safe_stem(self, raw: str, expected: str):
        assert slugify(raw) == expected

    def test_truncates_on_a_word_boundary(self):
        slug = slugify("senior staff principal distinguished backend engineer", limit=20)

        assert len(slug) <= 20
        assert not slug.endswith("-")

    def test_never_returns_an_empty_name(self):
        assert slugify("!!!") == "untitled"


class TestRenderCard:
    def _card(self, **overrides) -> Card:
        payload = {
            "number": 1,
            "company": "Canonical",
            "title": "Backend Developer",
            "location": "Remote",
            "score": 70,
            "posting_url": "https://example.com/job/1",
            "channel": ContactKind.ATS_FORM,
            "target": "https://example.com/apply/1",
            "draft": _draft(),
            "cv_path": "cv/CV_EN.pdf",
        }
        payload.update(overrides)
        return Card(**payload)  # type: ignore[arg-type]

    def test_each_copyable_field_stands_alone(self):
        # The workflow is copy-paste: a field wrapped in prose is a field the
        # sender has to select around.
        text = render_card(self._card())

        for block in (_draft().subject, _draft().body, "https://example.com/apply/1"):
            assert f"```\n{block}\n```" in text

    def test_a_web_form_card_says_open_this_link(self):
        assert "APPLY AT" in render_card(self._card())

    def test_an_email_card_says_to(self):
        card = self._card(channel=ContactKind.EMAIL, target="hr@acme.com")

        text = render_card(card)

        assert "TO (email address)" in text
        assert "hr@acme.com" in text

    def test_shows_the_cv_to_attach(self):
        assert "cv/CV_EN.pdf" in render_card(self._card())

    def test_tells_the_sender_how_to_record_it(self):
        assert "jobbot outbox sent 1" in render_card(self._card())

    def test_filename_is_numbered_and_descriptive(self):
        assert self._card().filename.startswith("001-canonical-")
        assert self._card().filename.endswith(".md")


class TestRenderIndex:
    def test_lists_every_card(self):
        cards = [
            Card(
                number=index,
                company=f"Co{index}",
                title="Backend Developer",
                location="Remote",
                score=70,
                posting_url="https://example.com",
                channel=ContactKind.ATS_FORM,
                target="https://example.com/apply",
                draft=_draft(),
                cv_path="cv/x.pdf",
            )
            for index in (1, 2)
        ]

        text = render_index(cards)

        assert "Co1" in text and "Co2" in text

    def test_reports_what_was_not_queued(self):
        text = render_index([], skipped={"already applied": 3})

        assert "already applied" in text and "3" in text


class TestBuild:
    def test_writes_a_card_and_an_index(self, session: Session, profile, settings, tmp_path):
        _job(session, "Acme")
        client = ScriptedClient(GOOD)

        report = build(
            client, profile=profile, session=session, settings=settings, directory=tmp_path
        )

        assert report.written == 1
        assert (tmp_path / "README.md").exists()
        assert len(list(tmp_path.glob("0*.md"))) == 1

    def test_records_an_application_pending_review(
        self, session: Session, profile, settings, tmp_path
    ):
        _job(session, "Acme")

        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )
        session.commit()

        application = session.exec(select(Application)).one()
        assert application.status is ApplicationStatus.PENDING_REVIEW
        assert application.subject == GOOD["subject"]

    def test_nothing_is_marked_sent_by_building(
        self, session: Session, profile, settings, tmp_path
    ):
        # Building prepares; only the human reports having sent.
        _job(session, "Acme")

        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )
        session.commit()

        assert session.exec(select(Application)).one().sent_at is None

    def test_respects_the_limit(self, session: Session, profile, settings, tmp_path):
        for name in ("Alpha", "Beta", "Gamma"):
            _job(session, name)

        report = build(
            ScriptedClient(GOOD, GOOD),
            profile=profile,
            session=session,
            settings=settings,
            limit=2,
            directory=tmp_path,
        )

        assert report.written == 2

    def test_two_postings_at_one_company_produce_one_card(
        self, session: Session, profile, settings, tmp_path
    ):
        # One application per company per month is a database constraint, and a
        # company routinely has several matching postings. Before this check the
        # second card raised IntegrityError and lost the whole batch.
        _, company = _job(session, "Acme", score=80)
        second = "https://boards.example.com/acme/jobs/2"
        session.add(
            Job(
                company_id=company.id,
                title="Senior Nothing",
                url=second,
                url_hash=url_fingerprint(second),
                location="Remote",
                raw_description=POSTING,
                match_score=75,
                status=JobStatus.CONTACT_PENDING,
            )
        )
        session.commit()

        report = build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            limit=5,
            directory=tmp_path,
        )

        assert report.written == 1
        assert report.skipped.get("already drafted this month") == 1

    def test_skips_a_blocked_company(self, session: Session, profile, settings, tmp_path):
        _, company = _job(session, "Acme")
        company.is_blocked = True
        session.add(company)
        session.commit()

        report = build(
            ScriptedClient(), profile=profile, session=session, settings=settings,
            directory=tmp_path,
        )

        assert report.written == 0
        assert report.skipped

    def test_skips_a_company_inside_its_cooldown(
        self, session: Session, profile, settings, tmp_path
    ):
        _, company = _job(session, "Acme")
        session.add(
            Application(
                company_id=company.id,
                period="2026-07",
                status=ApplicationStatus.SENT,
                sent_at=datetime.now(UTC) - timedelta(days=5),
            )
        )
        session.commit()

        report = build(
            ScriptedClient(), profile=profile, session=session, settings=settings,
            directory=tmp_path,
        )

        assert report.written == 0

    def test_uses_a_discovered_email_when_one_exists(
        self, session: Session, profile, settings, tmp_path
    ):
        _, company = _job(session, "Acme")
        session.add(
            Contact(company_id=company.id, kind=ContactKind.EMAIL, value="ik@acme.com")
        )
        session.commit()

        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )

        card = next(tmp_path.glob("0*.md")).read_text(encoding="utf-8")
        assert "ik@acme.com" in card
        assert "TO (email address)" in card

    def test_falls_back_to_the_posting_url(self, session: Session, profile, settings, tmp_path):
        _job(session, "Acme")

        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )

        card = next(tmp_path.glob("0*.md")).read_text(encoding="utf-8")
        assert "https://boards.example.com/acme/jobs/1" in card

    def test_a_rejected_draft_is_counted_not_written(
        self, session: Session, profile, settings, tmp_path
    ):
        _job(session, "Acme")
        bad = {"subject": "s", "body": "too short", "cited_detail": "invented entirely"}

        report = build(
            ScriptedClient(bad, bad, bad),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )

        assert report.written == 0
        assert "draft failed validation" in report.skipped

    def test_rebuilding_removes_stale_cards(self, session: Session, profile, settings, tmp_path):
        stale = tmp_path / "099-old-card.md"
        tmp_path.mkdir(parents=True, exist_ok=True)
        stale.write_text("old", encoding="utf-8")
        _job(session, "Acme")

        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )

        assert not stale.exists()


class TestMarkSent:
    def test_starts_the_cooldown(self, session: Session, profile, settings, tmp_path):
        job, _ = _job(session, "Acme")
        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )
        session.commit()
        number = session.exec(select(Application)).one().id

        application = mark_sent(session, number)
        session.commit()

        assert application.status is ApplicationStatus.SENT
        assert application.sent_at is not None
        assert session.get(Job, job.id).status is JobStatus.APPLIED

    def test_unknown_number_is_an_error(self, session: Session):
        with pytest.raises(LookupError, match="no application numbered 99"):
            mark_sent(session, 99)

    def test_marking_twice_is_refused(self, session: Session, profile, settings, tmp_path):
        _job(session, "Acme")
        build(
            ScriptedClient(GOOD),
            profile=profile,
            session=session,
            settings=settings,
            directory=tmp_path,
        )
        session.commit()
        number = session.exec(select(Application)).one().id
        mark_sent(session, number)
        session.commit()

        with pytest.raises(ValueError, match="already marked sent"):
            mark_sent(session, number)
