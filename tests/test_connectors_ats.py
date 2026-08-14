"""ATS connectors, driven by recorded response shapes rather than live APIs."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.connectors.ats import (
    PROVIDERS,
    AtsConnector,
    Board,
    build_connectors,
    detect_board,
)
from jobbot.net import HttpClient

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 4012345,
            "title": "Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
            "location": {"name": "Istanbul, Türkiye"},
            "content": "&lt;p&gt;Java, &lt;b&gt;Spring Boot&lt;/b&gt; and PostgreSQL&lt;/p&gt;",
            "updated_at": "2026-08-01T10:00:00Z",
        },
        {"id": 999, "title": "No URL", "location": {"name": "Remote"}},
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Junior Developer",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        "categories": {"location": "Ankara"},
        "descriptionPlain": "Spring Boot and Docker",
        "createdAt": 1754000000000,
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "ash-1",
            "title": "Software Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ash-1",
            "location": "Remote",
            "descriptionPlain": "Python and FastAPI",
            "publishedAt": "2026-07-15T08:30:00Z",
        }
    ]
}


@pytest.fixture
def board() -> Board:
    return Board(provider="greenhouse", token="acme", name="Acme", domain="acme.com", country="TR")


@pytest.fixture
def client(settings) -> HttpClient:
    return HttpClient(settings, client=httpx.AsyncClient())


class TestGreenhouse:
    @respx.mock
    async def test_maps_postings_and_strips_html(self, client: HttpClient, board: Board):
        url = PROVIDERS["greenhouse"].endpoint("acme")
        respx.get(url).mock(return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD))

        jobs = await AtsConnector(PROVIDERS["greenhouse"], [board]).fetch(client)

        assert len(jobs) == 1  # the entry without a URL is dropped
        job = jobs[0]
        assert job.title == "Backend Engineer"
        assert job.external_id == "4012345"
        assert job.ats_provider == "greenhouse"
        assert "<b>" not in job.description
        assert "Spring Boot" in job.description
        assert job.posted_at is not None

    @respx.mock
    async def test_posting_country_comes_from_the_posting_not_the_company(
        self, client: HttpClient
    ):
        # A US company with an Istanbul role must read as TR, and vice versa.
        us_company = Board("greenhouse", "acme", "Acme", "acme.com", country="US")
        respx.get(PROVIDERS["greenhouse"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
        )

        jobs = await AtsConnector(PROVIDERS["greenhouse"], [us_company]).fetch(client)

        assert jobs[0].country == "TR"


class TestLever:
    @respx.mock
    async def test_maps_bare_list_and_epoch_milliseconds(self, client: HttpClient):
        board = Board("lever", "acme", "Acme", "acme.com")
        respx.get(PROVIDERS["lever"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json=LEVER_PAYLOAD)
        )

        jobs = await AtsConnector(PROVIDERS["lever"], [board]).fetch(client)

        assert len(jobs) == 1
        assert jobs[0].apply_url.endswith("/apply")
        assert jobs[0].posted_at is not None
        assert jobs[0].posted_at.year == 2025


class TestAshby:
    @respx.mock
    async def test_detects_remote_from_location(self, client: HttpClient):
        board = Board("ashby", "acme", "Acme", "acme.com")
        respx.get(PROVIDERS["ashby"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json=ASHBY_PAYLOAD)
        )

        jobs = await AtsConnector(PROVIDERS["ashby"], [board]).fetch(client)

        assert jobs[0].is_remote is True


class TestFailureIsolation:
    @respx.mock
    async def test_one_dead_board_does_not_lose_the_others(self, client: HttpClient):
        good = Board("greenhouse", "acme", "Acme", "acme.com")
        bad = Board("greenhouse", "gone", "Gone", "gone.com")
        respx.get(PROVIDERS["greenhouse"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
        )
        respx.get(PROVIDERS["greenhouse"].endpoint("gone")).mock(
            return_value=httpx.Response(404, text="Not Found")
        )

        jobs = await AtsConnector(PROVIDERS["greenhouse"], [good, bad]).fetch(client)

        assert len(jobs) == 1

    @respx.mock
    async def test_malformed_payload_yields_nothing_instead_of_raising(self, client: HttpClient):
        board = Board("greenhouse", "acme", "Acme", "acme.com")
        respx.get(PROVIDERS["greenhouse"].endpoint("acme")).mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )

        assert await AtsConnector(PROVIDERS["greenhouse"], [board]).fetch(client) == []


class TestDetectBoard:
    @pytest.mark.parametrize(
        ("url", "provider", "token"),
        [
            ("https://boards.greenhouse.io/acme", "greenhouse", "acme"),
            ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse", "acme"),
            ("https://jobs.lever.co/acme/abc", "lever", "acme"),
            ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
            ("https://apply.workable.com/acme/", "workable", "acme"),
            ("https://acme.recruitee.com/o/backend", "recruitee", "acme"),
            ("https://jobs.smartrecruiters.com/Acme/12345", "smartrecruiters", "Acme"),
        ],
    )
    def test_recognises_ats_from_a_careers_url(self, url: str, provider: str, token: str):
        # A careers page that redirects to an ATS gives arm 2 an exact
        # application endpoint instead of a generic contact address.
        board = detect_board(url)

        assert board is not None
        assert (board.provider, board.token) == (provider, token)

    def test_ignores_the_vendors_own_subdomains(self):
        assert detect_board("https://www.workable.com/pricing") is None

    def test_returns_none_for_an_ordinary_careers_page(self):
        assert detect_board("https://acme.com.tr/kariyer") is None


class TestBuildConnectors:
    def test_groups_boards_by_provider(self):
        connectors = build_connectors(
            [
                Board("greenhouse", "a", "A", "a.com"),
                Board("greenhouse", "b", "B", "b.com"),
                Board("lever", "c", "C", "c.com"),
            ]
        )

        assert [connector.name for connector in connectors] == ["ats:greenhouse", "ats:lever"]
        assert len(connectors[0].boards) == 2

    def test_skips_unknown_providers_instead_of_crashing(self):
        connectors = build_connectors(
            [Board("myspace", "a", "A", "a.com"), Board("lever", "c", "C", "c.com")]
        )

        assert [connector.name for connector in connectors] == ["ats:lever"]
