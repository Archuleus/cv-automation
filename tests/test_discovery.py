"""Company discovery: the sources that grow the employer list on their own."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from jobbot.arms import companies as discovery
from jobbot.config import Settings
from jobbot.discovery.base import (
    DiscoveredCompany,
    deduplicate,
    is_company_domain,
    normalise_domain,
)
from jobbot.discovery.directories import Directory, DirectorySource, extract_companies
from jobbot.discovery.github_orgs import GitHubOrgSource
from jobbot.models import Company

DIRECTORY_PAGE = """
<html><body>
  <nav>
    <a href="/">Ana Sayfa</a>
    <a href="/hakkimizda">Hakkımızda</a>
    <a href="/iletisim">İletişim</a>
  </nav>
  <ul class="firmalar">
    <li><a href="https://acme-yazilim.com.tr">Acme Yazılım A.Ş.</a></li>
    <li><a href="http://www.beta-teknoloji.com">Beta Teknoloji</a></li>
    <li><a href="https://gamma.io/">Gamma Bilişim</a></li>
    <li><a href="https://acme-yazilim.com.tr/detay">Acme Yazılım (tekrar)</a></li>
  </ul>
  <footer>
    <a href="https://linkedin.com/company/teknopark">LinkedIn</a>
    <a href="https://www.sanayi.gov.tr">Bakanlık</a>
    <a href="https://kariyer.net/is-ilanlari">Kariyer.net</a>
  </footer>
</body></html>
"""


class TestNormaliseDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.Acme.com.TR/kariyer", "acme.com.tr"),
            ("acme.com.tr", "acme.com.tr"),
            ("http://acme.com:8080/x", "acme.com"),
            ("//acme.com", "acme.com"),
            ("", ""),
        ],
    )
    def test_reduces_to_a_bare_domain(self, raw: str, expected: str):
        assert normalise_domain(raw) == expected


class TestIsCompanyDomain:
    @pytest.mark.parametrize(
        "domain", ["acme.com.tr", "beta-teknoloji.com", "gamma.io"]
    )
    def test_accepts_a_company_site(self, domain: str):
        assert is_company_domain(domain)

    @pytest.mark.parametrize(
        "domain",
        [
            "linkedin.com",
            "www.facebook.com".removeprefix("www."),
            "sanayi.gov.tr",
            "kariyer.net",
            "",
            "localhost",
        ],
    )
    def test_rejects_platforms_agencies_and_job_boards(self, domain: str):
        # Every directory links to these; none of them is a company.
        assert not is_company_domain(domain)


class TestExtractCompanies:
    def test_finds_the_member_companies(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://teknopark.com.tr/firmalar",
            directory_name="Test Teknopark",
        )

        domains = {company.domain for company in found}
        assert domains == {"acme-yazilim.com.tr", "beta-teknoloji.com", "gamma.io"}

    def test_keeps_the_company_name_from_the_link(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://teknopark.com.tr/firmalar",
            directory_name="Test",
        )

        names = {company.domain: company.name for company in found}
        assert names["acme-yazilim.com.tr"] == "Acme Yazılım A.Ş."

    def test_drops_the_directorys_own_navigation(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://teknopark.com.tr/firmalar",
            directory_name="Test",
        )

        assert not any("teknopark.com.tr" in company.domain for company in found)

    def test_drops_social_agency_and_job_board_links(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://teknopark.com.tr/firmalar",
            directory_name="Test",
        )

        domains = {company.domain for company in found}
        assert not domains & {"linkedin.com", "sanayi.gov.tr", "kariyer.net"}

    def test_one_entry_per_domain(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://teknopark.com.tr/firmalar",
            directory_name="Test",
        )

        assert len(found) == len({company.domain for company in found})

    def test_records_which_directory_it_came_from(self):
        found = extract_companies(
            DIRECTORY_PAGE, base_url="https://x.com/f", directory_name="ODTÜ Teknokent"
        )

        assert all(c.source_ref == "directory:ODTÜ Teknokent" for c in found)

    def test_an_empty_page_yields_nothing(self):
        assert extract_companies("", base_url="https://x.com", directory_name="X") == []


class TestDeduplicate:
    def test_collapses_repeats_across_sources(self):
        companies = [
            DiscoveredCompany(name="Acme", domain="acme.com"),
            DiscoveredCompany(name="ACME A.Ş.", domain="https://www.acme.com/"),
        ]

        assert len(deduplicate(companies)) == 1

    def test_drops_unusable_entries(self):
        companies = [
            DiscoveredCompany(name="", domain="acme.com"),
            DiscoveredCompany(name="LinkedIn", domain="linkedin.com"),
            DiscoveredCompany(name="Good", domain="good.com.tr"),
        ]

        assert [c.domain for c in deduplicate(companies)] == ["good.com.tr"]


class FakeClient:
    def __init__(self, pages: dict[str, str], json_pages: dict[str, object] | None = None):
        self.pages = pages
        self.json_pages = json_pages or {}
        self.requested: list[str] = []

    async def get_text(self, url: str, **_: object) -> str:
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        raise RuntimeError(f"404 {url}")

    async def get_json(self, url: str, **kwargs: object) -> object:
        self.requested.append(url)
        if url in self.json_pages:
            return self.json_pages[url]
        raise RuntimeError(f"404 {url}")

    async def aclose(self) -> None: ...


class TestDirectorySource:
    async def test_harvests_a_directory(self):
        client = FakeClient({"https://tp.com.tr/firmalar": DIRECTORY_PAGE})
        source = DirectorySource((Directory(name="TP", url="https://tp.com.tr/firmalar"),))

        found = await source.discover(client)  # type: ignore[arg-type]

        assert len(found) == 3

    async def test_a_dead_directory_costs_one_request_not_the_run(self):
        client = FakeClient({"https://good.com/f": DIRECTORY_PAGE})
        source = DirectorySource(
            (
                Directory(name="Dead", url="https://dead.example/f"),
                Directory(name="Good", url="https://good.com/f"),
            )
        )

        found = await source.discover(client)  # type: ignore[arg-type]

        assert len(found) == 3


class TestGitHubOrgSource:
    async def test_keeps_only_organisations_with_a_website(self):
        client = FakeClient(
            {},
            {
                "https://api.github.com/search/users": {
                    "items": [{"login": "acme-tr"}, {"login": "nosite-tr"}]
                },
                "https://api.github.com/users/acme-tr": {
                    "name": "Acme Teknoloji",
                    "blog": "https://acme.com.tr",
                },
                "https://api.github.com/users/nosite-tr": {"name": "No Site", "blog": ""},
            },
        )
        source = GitHubOrgSource(locations=("Istanbul",), max_pages=1)

        found = await source.discover(client)  # type: ignore[arg-type]

        # An organisation with no website has no careers page for arm 2 to visit.
        assert [company.domain for company in found] == ["acme.com.tr"]

    async def test_records_the_github_login_as_provenance(self):
        client = FakeClient(
            {},
            {
                "https://api.github.com/search/users": {"items": [{"login": "acme-tr"}]},
                "https://api.github.com/users/acme-tr": {
                    "name": "Acme",
                    "blog": "acme.com.tr",
                },
            },
        )

        found = await GitHubOrgSource(locations=("Ankara",), max_pages=1).discover(
            client  # type: ignore[arg-type]
        )

        assert found[0].source_ref == "github:acme-tr"


class StubSource:
    def __init__(self, name: str, companies: list[DiscoveredCompany], fail: bool = False):
        self.name = name
        self._companies = companies
        self._fail = fail

    async def discover(self, client: object) -> list[DiscoveredCompany]:
        if self._fail:
            raise RuntimeError("source is down")
        return self._companies


class TestDiscoveryRun:
    @pytest.fixture
    def settings(self) -> Settings:
        return Settings(_env_file=None, database_url="sqlite://")

    async def test_stores_new_companies_unvisited(self, session: Session, settings):
        source = StubSource("stub", [DiscoveredCompany(name="Acme", domain="acme.com.tr")])

        report = await discovery.run(
            session=session, settings=settings, sources=[source], client=FakeClient({})  # type: ignore[arg-type,list-item]
        )

        assert report.added == 1
        company = session.exec(select(Company)).one()
        # Unvisited is what makes arm 2 pick it up.
        assert company.investigated_at is None

    async def test_does_not_duplicate_a_known_company(self, session: Session, settings):
        session.add(Company(name="Acme", domain="acme.com.tr"))
        session.commit()
        source = StubSource("stub", [DiscoveredCompany(name="Acme A.Ş.", domain="acme.com.tr")])

        report = await discovery.run(
            session=session, settings=settings, sources=[source], client=FakeClient({})  # type: ignore[arg-type,list-item]
        )

        assert (report.added, report.already_known) == (0, 1)
        assert len(session.exec(select(Company)).all()) == 1

    async def test_one_failing_source_does_not_lose_the_others(
        self, session: Session, settings
    ):
        sources = [
            StubSource("broken", [], fail=True),
            StubSource("working", [DiscoveredCompany(name="Acme", domain="acme.com.tr")]),
        ]

        report = await discovery.run(
            session=session, settings=settings, sources=sources, client=FakeClient({})  # type: ignore[arg-type,list-item]
        )

        assert report.added == 1
        assert report.per_source == {"broken": 0, "working": 1}

    def test_the_shipped_example_directory_list_parses(self):
        # What a new user copies to get started.
        from jobbot.config import PROJECT_ROOT
        from jobbot.discovery.directories import load_directories

        directories = load_directories(PROJECT_ROOT / "data" / "directories.example.json")

        assert directories
        assert all(d.url.startswith("https://") for d in directories)
