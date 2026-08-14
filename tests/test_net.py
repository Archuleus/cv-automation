from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from jobbot import net
from jobbot.config import Settings
from jobbot.net import (
    DomainRateLimiter,
    FetchError,
    HttpClient,
    RequestKind,
    RobotsDisallowedError,
    chunked,
    user_agent,
)


@pytest.fixture
def client(settings: Settings) -> HttpClient:
    return HttpClient(settings, client=httpx.AsyncClient())


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff waits are correctness, not something to sit through in tests."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(net.asyncio, "sleep", fake_sleep)
    return recorded


class TestUserAgent:
    def test_identifies_the_client_and_a_contact(self):
        agent = user_agent(Settings(_env_file=None, applicant_email="a@b.com"))

        assert agent.startswith("jobbot/")
        assert "a@b.com" in agent

    def test_falls_back_when_no_contact_is_configured(self):
        assert "unspecified" in user_agent(Settings(_env_file=None, applicant_email=""))


class TestDomainRateLimiter:
    def test_rejects_a_nonpositive_rate(self):
        with pytest.raises(ValueError):
            DomainRateLimiter(0)

    async def test_spaces_consecutive_requests_to_one_host(self, no_real_sleeping):
        limiter = DomainRateLimiter(requests_per_second=2)  # 0.5s apart

        async with limiter.slot("example.com"):
            pass
        async with limiter.slot("example.com"):
            pass

        assert no_real_sleeping and no_real_sleeping[-1] == pytest.approx(0.5, abs=0.05)

    async def test_does_not_delay_a_different_host(self, no_real_sleeping):
        limiter = DomainRateLimiter(requests_per_second=2)

        async with limiter.slot("a.com"):
            pass
        async with limiter.slot("b.com"):
            pass

        assert no_real_sleeping == []

    async def test_serialises_concurrent_requests_to_one_host(self):
        limiter = DomainRateLimiter(requests_per_second=1000)
        active = 0
        peak = 0

        async def worker() -> None:
            nonlocal active, peak
            async with limiter.slot("example.com"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(5)))

        assert peak == 1


class TestRobots:
    @respx.mock
    async def test_page_fetch_is_blocked_when_disallowed(self, client: HttpClient):
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
        )

        with pytest.raises(RobotsDisallowedError):
            await client.get("https://example.com/private/page", kind=RequestKind.PAGE)

    @respx.mock
    async def test_page_fetch_is_allowed_when_permitted(self, client: HttpClient):
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
        )
        respx.get("https://example.com/careers").mock(return_value=httpx.Response(200, text="ok"))

        assert await client.get_text("https://example.com/careers") == "ok"

    @respx.mock
    async def test_missing_robots_txt_means_no_restrictions(self, client: HttpClient):
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://example.com/careers").mock(return_value=httpx.Response(200, text="ok"))

        assert await client.get_text("https://example.com/careers") == "ok"

    @respx.mock
    async def test_api_calls_skip_robots_entirely(self, client: HttpClient):
        # A documented JSON endpoint is not crawling, and no robots.txt request
        # should even be made for it.
        robots = respx.get("https://api.example.com/robots.txt")
        respx.get("https://api.example.com/v1/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )

        await client.get_json("https://api.example.com/v1/jobs")

        assert not robots.called

    @respx.mock
    async def test_robots_txt_is_fetched_once_per_host(self, client: HttpClient):
        robots = respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
        )
        respx.get("https://example.com/a").mock(return_value=httpx.Response(200, text="a"))
        respx.get("https://example.com/b").mock(return_value=httpx.Response(200, text="b"))

        await client.get_text("https://example.com/a")
        await client.get_text("https://example.com/b")

        assert robots.call_count == 1


class TestRetries:
    @respx.mock
    async def test_retries_then_succeeds(self, client: HttpClient):
        route = respx.get("https://api.example.com/v1/jobs")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json={"jobs": [1]}),
        ]

        assert await client.get_json("https://api.example.com/v1/jobs") == {"jobs": [1]}
        assert route.call_count == 2

    @respx.mock
    async def test_honours_retry_after(self, client: HttpClient, no_real_sleeping):
        route = respx.get("https://api.example.com/v1/jobs")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]

        await client.get_json("https://api.example.com/v1/jobs")

        # Retrying harder against a server that asked for a pause is how a
        # polite client becomes an abusive one.
        assert 7.0 in no_real_sleeping

    @respx.mock
    async def test_ignores_an_unparseable_retry_after(self, client: HttpClient):
        route = respx.get("https://api.example.com/v1/jobs")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={}),
        ]

        await client.get_json("https://api.example.com/v1/jobs")

        assert route.call_count == 2

    @respx.mock
    async def test_gives_up_after_the_attempt_limit(self, client: HttpClient):
        respx.get("https://api.example.com/v1/jobs").mock(return_value=httpx.Response(503))

        with pytest.raises(FetchError, match="giving up"):
            await client.get_json("https://api.example.com/v1/jobs")

    @respx.mock
    async def test_does_not_retry_a_permanent_failure(self, client: HttpClient):
        route = respx.get("https://api.example.com/v1/jobs").mock(
            return_value=httpx.Response(404)
        )

        with pytest.raises(FetchError):
            await client.get_json("https://api.example.com/v1/jobs")

        assert route.call_count == 1

    @respx.mock
    async def test_a_probe_does_not_retry(self, client: HttpClient):
        # Guessing eight paths on a dead domain at four attempts each turns one
        # unreachable site into minutes of waiting.
        route = respx.get("https://example.com/kariyer")
        route.side_effect = httpx.ConnectError("refused")
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))

        with pytest.raises(FetchError):
            await client.get_text("https://example.com/kariyer", attempts=1)

        assert route.call_count == 1

    @respx.mock
    async def test_retries_connection_errors(self, client: HttpClient):
        route = respx.get("https://api.example.com/v1/jobs")
        route.side_effect = [httpx.ConnectError("boom"), httpx.Response(200, json={})]

        await client.get_json("https://api.example.com/v1/jobs")

        assert route.call_count == 2


class TestPayloads:
    @respx.mock
    async def test_non_json_response_is_an_error(self, client: HttpClient):
        respx.get("https://api.example.com/v1/jobs").mock(
            return_value=httpx.Response(200, text="<html>nope</html>")
        )

        with pytest.raises(FetchError, match="not JSON"):
            await client.get_json("https://api.example.com/v1/jobs")


class TestLifecycle:
    @respx.mock
    async def test_context_manager_closes_its_own_client(self, settings: Settings):
        async with HttpClient(settings) as owned:
            assert owned is not None

    async def test_does_not_close_a_borrowed_client(self, settings: Settings):
        borrowed = httpx.AsyncClient()
        await HttpClient(settings, client=borrowed).aclose()

        assert not borrowed.is_closed
        await borrowed.aclose()


class TestChunked:
    def test_splits_into_batches(self):
        assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]

    def test_empty_input_yields_nothing(self):
        assert list(chunked([], 3)) == []
