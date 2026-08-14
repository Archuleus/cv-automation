"""Arm 1: deduplication, scoring gates, and persistence."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlmodel import Session, select

from jobbot.arms import discover
from jobbot.arms.discover import DiscoveryReport, company_domain, deduplicate, persist
from jobbot.config import Settings
from jobbot.connectors.base import RawCompany, RawJob
from jobbot.models import Company, Event, Job, JobStatus, SourceKind

GOOD_DESCRIPTION = "Java, Spring Boot, Spring Data JPA, PostgreSQL, Docker, REST API, JWT"


def make_job(
    *,
    title: str = "Junior Backend Developer",
    url: str = "https://acme.com.tr/jobs/1",
    location: str = "Istanbul, Türkiye",
    description: str = GOOD_DESCRIPTION,
    domain: str = "acme.com.tr",
    name: str = "Acme",
) -> RawJob:
    return RawJob(
        company=RawCompany(name=name, domain=domain, source=SourceKind.ATS),
        title=title,
        url=url,
        location=location,
        description=description,
        ats_provider="greenhouse",
        external_id=url.rsplit("/", 1)[-1],
    )


class StubConnector:
    source = SourceKind.ATS

    def __init__(self, name: str, jobs: Sequence[RawJob] | None = None, fail: bool = False) -> None:
        self.name = name
        self._jobs = list(jobs or [])
        self._fail = fail

    async def fetch(self, client: object) -> Sequence[RawJob]:
        if self._fail:
            raise RuntimeError("source is down")
        return self._jobs


class TestCompanyDomain:
    def test_prefers_the_declared_domain(self):
        assert company_domain(make_job(domain="Acme.com.TR")) == "acme.com.tr"

    def test_strips_www(self):
        assert company_domain(make_job(domain="www.acme.com")) == "acme.com"

    def test_falls_back_to_the_posting_host(self):
        job = make_job(domain="", url="https://www.careers.acme.io/jobs/9")

        assert company_domain(job) == "careers.acme.io"

    def test_never_uses_an_ats_vendor_host_as_the_company_identity(self):
        # boards.greenhouse.io identifies the vendor, not the employer. Using it
        # would collapse every domain-less company into one row.
        alpha = RawJob(
            company=RawCompany(name="Alpha", domain="", source_ref="greenhouse:alpha"),
            title="Backend Developer",
            url="https://boards.greenhouse.io/alpha/jobs/1",
        )
        beta = RawJob(
            company=RawCompany(name="Beta", domain="", source_ref="greenhouse:beta"),
            title="Backend Developer",
            url="https://boards.greenhouse.io/beta/jobs/1",
        )

        assert company_domain(alpha) != company_domain(beta)

    @pytest.mark.parametrize(
        "url",
        [
            "https://jobs.lever.co/acme/1",
            "https://jobs.ashbyhq.com/acme/1",
            "https://apply.workable.com/acme/j/1/",
            "https://acme.recruitee.com/o/backend",
            "https://jobs.smartrecruiters.com/Acme/1",
        ],
    )
    def test_every_ats_vendor_host_is_recognised(self, url: str):
        job = RawJob(
            company=RawCompany(name="Acme", domain="", source_ref="lever:acme"),
            title="Backend Developer",
            url=url,
        )

        assert company_domain(job) == "lever:acme"

    def test_domainless_companies_stay_separate_in_the_database(
        self, session: Session, profile, settings
    ):
        jobs = [
            RawJob(
                company=RawCompany(name=name, domain="", source_ref=f"greenhouse:{name}"),
                title="Junior Backend Developer",
                url=f"https://boards.greenhouse.io/{name}/jobs/1",
                location="Istanbul, Türkiye",
                description=GOOD_DESCRIPTION,
            )
            for name in ("alpha", "beta")
        ]

        persist(session, jobs, profile, settings, DiscoveryReport())
        session.commit()

        assert len(session.exec(select(Company)).all()) == 2


class TestDeduplicate:
    def test_drops_identical_urls(self):
        unique, duplicates = deduplicate([make_job(), make_job()])

        assert (len(unique), duplicates) == (1, 1)

    def test_drops_near_identical_titles_at_one_company(self):
        # The same role listed on the company site and on an aggregator.
        jobs = [
            make_job(url="https://acme.com.tr/jobs/1", title="Backend Developer"),
            make_job(url="https://aggregator.com/x", title="Backend Developer (Remote)"),
        ]

        unique, duplicates = deduplicate(jobs)

        assert (len(unique), duplicates) == (1, 1)

    def test_keeps_different_roles_at_one_company(self):
        jobs = [
            make_job(url="https://acme.com.tr/jobs/1", title="Backend Developer"),
            make_job(url="https://acme.com.tr/jobs/2", title="Mobile Developer"),
        ]

        unique, _ = deduplicate(jobs)

        assert len(unique) == 2

    def test_keeps_the_same_title_at_different_companies(self):
        jobs = [
            make_job(url="https://a.com/1", domain="a.com"),
            make_job(url="https://b.com/1", domain="b.com"),
        ]

        unique, duplicates = deduplicate(jobs)

        assert (len(unique), duplicates) == (2, 0)

    def test_empty_input(self):
        assert deduplicate([]) == ([], 0)


class TestPersist:
    @pytest.fixture
    def settings(self) -> Settings:
        return Settings(_env_file=None, database_url="sqlite://", min_match_score=55)

    def test_stores_a_matching_posting(self, session: Session, profile, settings):
        report = persist(session, [make_job()], profile, settings, DiscoveryReport())
        session.commit()

        assert report.stored == 1
        job = session.exec(select(Job)).one()
        assert job.match_score is not None and job.match_score >= 55
        assert job.status is JobStatus.CONTACT_PENDING
        assert job.match_reason

    def test_creates_the_company_once_for_several_postings(
        self, session: Session, profile, settings
    ):
        jobs = [
            make_job(url="https://acme.com.tr/jobs/1", title="Backend Developer"),
            make_job(url="https://acme.com.tr/jobs/2", title="Full Stack Developer"),
        ]

        persist(session, jobs, profile, settings, DiscoveryReport())
        session.commit()

        assert len(session.exec(select(Company)).all()) == 1
        assert len(session.exec(select(Job)).all()) == 2

    def test_rejected_postings_are_counted_not_stored(self, session: Session, profile, settings):
        report = persist(
            session, [make_job(title="Senior Java Developer")], profile, settings, DiscoveryReport()
        )
        session.commit()

        assert (report.stored, report.rejected) == (0, 1)
        assert session.exec(select(Job)).all() == []
        # Grouped by rule so a bad rule is visible, not by the value that fired.
        assert "seniority" in report.reject_reasons

    def test_postings_below_the_threshold_are_counted_separately(
        self, session: Session, profile, settings
    ):
        strict = Settings(_env_file=None, database_url="sqlite://", min_match_score=95)

        report = persist(session, [make_job()], profile, strict, DiscoveryReport())
        session.commit()

        assert (report.stored, report.below_threshold) == (0, 1)

    def test_a_known_url_is_skipped(self, session: Session, profile, settings):
        persist(session, [make_job()], profile, settings, DiscoveryReport())
        session.commit()

        report = persist(session, [make_job()], profile, settings, DiscoveryReport())
        session.commit()

        assert (report.stored, report.already_known) == (0, 1)

    def test_blocked_companies_are_skipped(self, session: Session, profile, settings):
        session.add(Company(name="Acme", domain="acme.com.tr", is_blocked=True))
        session.commit()

        report = persist(session, [make_job()], profile, settings, DiscoveryReport())
        session.commit()

        assert (report.stored, report.blocked_companies) == (0, 1)
        assert session.exec(select(Job)).all() == []

    def test_writes_an_audit_trail(self, session: Session, profile, settings):
        persist(session, [make_job()], profile, settings, DiscoveryReport())
        session.commit()

        recorded = {event.event for event in session.exec(select(Event)).all()}

        assert {"discovered", "scored"} <= recorded

    def test_carries_forward_counters_from_an_earlier_batch(
        self, session: Session, profile, settings
    ):
        earlier = DiscoveryReport(fetched=10, duplicates=3, rejected=2)

        report = persist(session, [make_job()], profile, settings, earlier)
        session.commit()

        assert (report.fetched, report.duplicates, report.rejected) == (10, 3, 2)
        assert report.stored == 1


class TestCollect:
    async def test_gathers_from_every_connector(self):
        connectors = [
            StubConnector("a", [make_job(url="https://a.com/1")]),
            StubConnector("b", [make_job(url="https://b.com/1")]),
        ]

        jobs, per_connector = await discover.collect(connectors, client=None)  # type: ignore[arg-type]

        assert len(jobs) == 2
        assert per_connector == {"a": 1, "b": 1}

    async def test_a_failing_connector_does_not_abort_the_run(self):
        connectors = [
            StubConnector("broken", fail=True),
            StubConnector("working", [make_job()]),
        ]

        jobs, per_connector = await discover.collect(connectors, client=None)  # type: ignore[arg-type]

        assert len(jobs) == 1
        assert per_connector == {"broken": 0, "working": 1}


class TestReport:
    def test_summary_lists_the_counters(self):
        summary = DiscoveryReport(fetched=100, duplicates=10, stored=5).summary

        assert "fetched=100" in summary
        assert "stored=5" in summary
