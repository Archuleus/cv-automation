"""The Ollama client, driven by recorded responses rather than a live model."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.config import Settings
from jobbot.llm.base import LlmError, LlmUnavailableError
from jobbot.llm.ollama import OllamaClient, strip_thinking

HOST = "http://localhost:11434"
SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}}}


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, llm_host=HOST, llm_model="qwen3:8b")


@pytest.fixture
def client(settings: Settings) -> OllamaClient:
    return OllamaClient(settings, client=httpx.Client(base_url=HOST))


def _tags(*names: str) -> dict[str, list[dict[str, str]]]:
    return {"models": [{"name": name} for name in names]}


class TestStripThinking:
    def test_removes_a_reasoning_block(self):
        assert strip_thinking("<think>plan the letter</think>Dear team") == "Dear team"

    def test_removes_a_multiline_block(self):
        assert strip_thinking("<think>\nstep 1\nstep 2\n</think>\nHello") == "Hello"

    def test_removes_orphan_tags(self):
        # A truncated generation can leave one tag without its partner.
        assert strip_thinking("</think>Hello").strip() == "Hello"

    def test_leaves_clean_text_alone(self):
        assert strip_thinking("Hello there") == "Hello there"


class TestHealth:
    @respx.mock
    def test_reports_ready_when_the_model_is_installed(self, client: OllamaClient):
        respx.get(f"{HOST}/api/tags").mock(
            return_value=httpx.Response(200, json=_tags("qwen3:8b", "qwen3:1.7b"))
        )

        assert "qwen3:8b" in client.health()

    @respx.mock
    def test_says_which_command_pulls_a_missing_model(self, client: OllamaClient):
        respx.get(f"{HOST}/api/tags").mock(
            return_value=httpx.Response(200, json=_tags("qwen3:1.7b"))
        )

        with pytest.raises(LlmUnavailableError, match="ollama pull qwen3:8b"):
            client.health()

    @respx.mock
    def test_says_how_to_start_a_stopped_server(self, client: OllamaClient):
        respx.get(f"{HOST}/api/tags").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(LlmUnavailableError, match="ollama serve"):
            client.health()

    @respx.mock
    def test_matches_an_untagged_model_name(self, settings: Settings):
        # "qwen3" in config should match "qwen3:latest" from the server.
        untagged = Settings(_env_file=None, llm_host=HOST, llm_model="qwen3")
        client = OllamaClient(untagged, client=httpx.Client(base_url=HOST))
        respx.get(f"{HOST}/api/tags").mock(
            return_value=httpx.Response(200, json=_tags("qwen3:latest"))
        )

        assert client.health()


class TestGenerateJson:
    @respx.mock
    def test_parses_a_json_response_and_reports_usage(self, client: OllamaClient):
        respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "response": '{"subject": "Backend Engineer"}',
                    "prompt_eval_count": 2400,
                    "eval_count": 380,
                },
            )
        )

        payload, stats = client.generate_json(system="s", prompt="p", schema=SCHEMA)

        assert payload == {"subject": "Backend Engineer"}
        assert (stats.prompt_tokens, stats.output_tokens) == (2400, 380)
        assert stats.model == "qwen3:8b"

    @respx.mock
    def test_strips_leaked_reasoning_before_parsing(self, client: OllamaClient):
        # Qwen3 is a reasoning model; a leaked block would break json.loads.
        respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(
                200, json={"response": '<think>hmm</think>{"subject": "ok"}'}
            )
        )

        payload, _ = client.generate_json(system="s", prompt="p", schema=SCHEMA)

        assert payload == {"subject": "ok"}

    @respx.mock
    def test_sends_the_schema_as_a_grammar_constraint(self, client: OllamaClient):
        route = respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "{}"})
        )

        client.generate_json(system="s", prompt="p", schema=SCHEMA)

        import json as json_module

        sent = json_module.loads(route.calls[0].request.content)
        assert sent["format"] == SCHEMA
        assert sent["stream"] is False
        assert sent["think"] is False

    @respx.mock
    def test_non_json_output_is_an_error(self, client: OllamaClient):
        respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "Sure! Here you go:"})
        )

        with pytest.raises(LlmError, match="not JSON"):
            client.generate_json(system="s", prompt="p", schema=SCHEMA)

    @respx.mock
    def test_empty_output_is_an_error(self, client: OllamaClient):
        respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "   "})
        )

        with pytest.raises(LlmError, match="empty"):
            client.generate_json(system="s", prompt="p", schema=SCHEMA)

    @respx.mock
    def test_a_json_array_is_an_error(self, client: OllamaClient):
        respx.post(f"{HOST}/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "[1, 2, 3]"})
        )

        with pytest.raises(LlmError, match="expected an object"):
            client.generate_json(system="s", prompt="p", schema=SCHEMA)

    @respx.mock
    def test_transport_failure_is_an_error(self, client: OllamaClient):
        respx.post(f"{HOST}/api/generate").mock(side_effect=httpx.ReadTimeout("slow"))

        with pytest.raises(LlmError, match="generation failed"):
            client.generate_json(system="s", prompt="p", schema=SCHEMA)
