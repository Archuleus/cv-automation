"""Arm 2: the pipeline that turns a company domain into a way to apply."""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from jobbot.arms import contact
from jobbot.arms.contact import CompanyFindings, SeedCompany, investigate, load_seed, persist
from jobbot.config import PROJECT_ROOT, Settings
from jobbot.models import Company, Contact, ContactKind, SourceKind

CAREERS_HTML = """
<html><body>
  <h1>Açık Pozisyonlar</h1>
  <p>Ekibimize katılmak için başvurun.</p>
  <a href="mailto:ik@acme.com.tr">ik@acme.com.tr</a>
</body></html>
"""

HOMEPAGE_HTML = """
<html><body>
  <a href="/urunler">Ürünler</a>
  <a href="/bize-katil">Bize Katıl</a>
</body></html>
"""

ATS_HOMEPAGE = """
<html><body>
  <a href="https://boards.greenhouse.io/acmetr">Careers</a>
</body></html>
"""

NOT_CAREERS = "<html><body><h1>Acme</h1><p>Ürünlerimiz.</p></body></html>"


class FakeClient:
    """Answers a fixed map of URLs; everything else is a dead link."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def get_text(self, url: str, **_: object) -> str:
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        raise RuntimeError(f"404 {url}")

    async def aclose(self) -> None: ...


ACME = SeedCompany(name="Acme", domain="acme.com.tr", sector="saas")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite://")


class TestLoadSeed:
    def test_reads_companies(self, tmp_path):
        path = tmp_path / "seed.json"
        path.write_text(
            json.dumps({"companies": [{"name": "Acme", "domain": "WWW.Acme.com.TR"}]}),
            encoding="utf-8",
        )

        seed = load_seed(path)

        assert len(seed) == 1
        assert seed[0].domain == "acme.com.tr"

    def test_skips_entries_missing_a_domain(self, tmp_path):
        path = tmp_path / "seed.json"
        path.write_text(json.dumps({"companies": [{"name": "Acme"}]}), encoding="utf-8")

        assert load_seed(path) == ()

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_seed(tmp_path / "absent.json") == ()

    def test_the_shipped_example_loads(self):
        # The example is what a new user copies; a typo in it would otherwise
        # surface only when they first run the arm.
        seed = load_seed(PROJECT_ROOT / "data" / "companies.example.json")

        assert seed
        assert all(company.domain and "." in company.domain for company in seed)
        assert len({company.domain for company in seed}) == len(seed)

    def test_the_configured_seed_loads_if_present(self):
        # Personal registries are not committed, so this only runs locally.
        path = PROJECT_ROOT / "data" / "tr_companies.json"
        if not path.exists():
            pytest.skip("data/tr_companies.json is absent; copy companies.example.json")

        seed = load_seed(path)

        assert len({company.domain for company in seed}) == len(seed)


class TestInvestigate:
    async def test_finds_a_careers_page_by_guessing_a_turkish_path(self):
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.careers_url.endswith("/kariyer")
        assert findings.outcome == "email found"

    async def test_extracts_the_published_hiring_address(self):
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.emails[0][0] == "ik@acme.com.tr"

    async def test_falls_back_to_the_homepage_when_no_path_works(self):
        client = FakeClient(
            {
                "https://acme.com.tr": HOMEPAGE_HTML,
                "https://acme.com.tr/bize-katil": CAREERS_HTML,
            }
        )

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.careers_url.endswith("/bize-katil")

    async def test_a_soft_404_is_not_mistaken_for_a_careers_page(self):
        # Many sites answer every path with 200 and their homepage.
        client = FakeClient({url: NOT_CAREERS for url in contact.candidate_urls("acme.com.tr")})

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.careers_url == ""
        assert findings.outcome == "no careers page"

    async def test_detects_an_ats_behind_the_careers_link(self):
        # The best possible outcome: a permanent machine-readable job feed.
        client = FakeClient({"https://acme.com.tr": ATS_HOMEPAGE})

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.board is not None
        assert (findings.board.provider, findings.board.token) == ("greenhouse", "acmetr")
        assert findings.outcome == "ats detected"

    async def test_a_detected_board_carries_the_company_identity(self):
        client = FakeClient({"https://acme.com.tr": ATS_HOMEPAGE})

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.board is not None
        assert findings.board.name == "Acme"
        assert findings.board.domain == "acme.com.tr"
        assert findings.board.country == "TR"

    async def test_checks_contact_pages_when_the_careers_page_has_no_address(self):
        client = FakeClient(
            {
                "https://acme.com.tr/kariyer": "<h1>Açık Pozisyonlar</h1><p>Başvurun</p>",
                "https://acme.com.tr/iletisim": '<a href="mailto:ik@acme.com.tr">yaz</a>',
            }
        )

        findings = await investigate(client, ACME)  # type: ignore[arg-type]

        assert findings.emails[0][0] == "ik@acme.com.tr"

    async def test_an_unreachable_site_is_reported_not_raised(self):
        findings = await investigate(FakeClient({}), ACME)  # type: ignore[arg-type]

        assert findings.outcome == "no careers page"

    async def test_stops_guessing_once_a_page_is_found(self):
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        await investigate(client, ACME)  # type: ignore[arg-type]

        # /kariyer is first; the rest of the path list must not be tried.
        assert client.requested[0].endswith("/kariyer")
        assert not any(url.endswith("/careers") for url in client.requested)


class TestPersist:
    def test_creates_the_company_with_a_turkish_origin(self, session: Session):
        persist(session, CompanyFindings(company=ACME, careers_url="https://acme.com.tr/kariyer"))
        session.commit()

        company = session.exec(select(Company)).one()
        assert company.domain == "acme.com.tr"
        assert company.source is SourceKind.TR_REGISTRY
        assert company.country == "TR"

    def test_stores_each_route_as_its_own_contact(self, session: Session):
        persist(
            session,
            CompanyFindings(
                company=ACME,
                careers_url="https://acme.com.tr/kariyer",
                emails=(("ik@acme.com.tr", 0.95),),
            ),
        )
        session.commit()

        kinds = {contact_row.kind for contact_row in session.exec(select(Contact)).all()}
        assert kinds == {ContactKind.CAREER_PAGE, ContactKind.EMAIL}

    def test_running_twice_does_not_duplicate_contacts(self, session: Session):
        findings = CompanyFindings(
            company=ACME,
            careers_url="https://acme.com.tr/kariyer",
            emails=(("ik@acme.com.tr", 0.95),),
        )

        persist(session, findings)
        session.commit()
        persist(session, findings)
        session.commit()

        assert len(session.exec(select(Contact)).all()) == 2
        assert len(session.exec(select(Company)).all()) == 1

    def test_records_a_company_even_when_nothing_was_found(self, session: Session):
        # Knowing a site yielded nothing is worth storing; it stops the next run
        # from treating it as unexplored.
        persist(session, CompanyFindings(company=ACME, error="no careers page"))
        session.commit()

        assert session.exec(select(Company)).one().domain == "acme.com.tr"
        assert session.exec(select(Contact)).all() == []


class TestRun:
    async def test_reports_an_outcome_per_company(self, session: Session, settings, tmp_path):
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        report = await contact.run(
            settings=settings, seed=(ACME,), session=session, client=client  # type: ignore[arg-type]
        )

        assert report.processed == 1
        assert report.careers_pages == 1
        assert report.emails_found == 1
        assert report.outcomes == {"email found": 1}

    async def test_feeds_a_detected_board_back_into_arm_1(
        self, session: Session, settings, tmp_path, monkeypatch
    ):
        # This is the compounding step: one page fetch becomes a permanent
        # source that arm 1 polls on every later run.
        registry = tmp_path / "boards.json"
        registry.write_text('{"boards": []}', encoding="utf-8")
        monkeypatch.setenv("JOBBOT_BOARDS_PATH", str(registry))
        from jobbot.config import get_settings

        get_settings.cache_clear()

        client = FakeClient({"https://acme.com.tr": ATS_HOMEPAGE})
        try:
            report = await contact.run(
                settings=settings, seed=(ACME,), session=session, client=client  # type: ignore[arg-type]
            )
        finally:
            get_settings.cache_clear()

        assert report.ats_detected == 1
        assert report.boards_added == 1
        assert "acmetr" in registry.read_text(encoding="utf-8")

    async def test_one_broken_site_does_not_end_the_run(self, session: Session, settings):
        broken = SeedCompany(name="Broken", domain="broken.example")
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        report = await contact.run(
            settings=settings,
            seed=(broken, ACME),
            session=session,
            client=client,  # type: ignore[arg-type]
        )

        assert report.processed == 2
        assert report.careers_pages == 1

    async def test_findings_are_committed_per_company(self, session: Session, settings):
        # The real crawl takes ten minutes; a failure near the end must not
        # discard everything found before it.
        client = FakeClient({"https://acme.com.tr/kariyer": CAREERS_HTML})

        await contact.run(
            settings=settings, seed=(ACME,), session=session, client=client  # type: ignore[arg-type]
        )
        session.rollback()

        assert session.exec(select(Company)).one().domain == "acme.com.tr"

    async def test_limit_bounds_the_crawl(self, session: Session, settings):
        client = FakeClient({})

        report = await contact.run(
            settings=settings,
            seed=(ACME, SeedCompany(name="B", domain="b.com")),
            session=session,
            limit=1,
            client=client,  # type: ignore[arg-type]
        )

        assert report.processed == 1
