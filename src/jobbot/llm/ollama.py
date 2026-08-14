"""Ollama-backed model client.

Everything runs on this machine, which changes the failure modes rather than
removing them. There is no API key to forget, no bill and no rate limit -- but
the model may simply not be pulled, the server may not be running, and a 4 GB
GPU means a single generation can take minutes. So the timeouts are long, the
errors say exactly which command fixes them, and generation is serial: two
concurrent requests on this hardware are slower than two sequential ones.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from jobbot.config import Settings, get_settings
from jobbot.llm.base import GenerationStats, LlmError, LlmUnavailableError

logger = logging.getLogger("jobbot.llm.ollama")

# Reasoning models (Qwen3 among them) wrap their scratchpad in these. Ollama is
# asked not to emit any, but a leaked block must never reach a recipient.
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
ORPHAN_THINK = re.compile(r"</?think>", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove reasoning scaffolding a model leaked into its answer."""
    without_blocks = THINK_BLOCK.sub(" ", text)
    return ORPHAN_THINK.sub(" ", without_blocks).strip()


class OllamaClient:
    """Talks to a local Ollama server."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.model = self._settings.llm_model
        self._host = self._settings.llm_host.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._host,
            timeout=httpx.Timeout(float(self._settings.llm_timeout_seconds), connect=5.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def installed_models(self) -> list[str]:
        try:
            response = self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LlmUnavailableError(
                f"no Ollama server at {self._host}. Start it with 'ollama serve', "
                "or set JOBBOT_LLM_HOST if it listens elsewhere."
            ) from error

        payload = response.json()
        return [
            str(entry.get("name", ""))
            for entry in payload.get("models", [])
            if isinstance(entry, dict) and entry.get("name")
        ]

    def health(self) -> str:
        """Confirm the configured model is pulled, or say how to pull it."""
        installed = self.installed_models()
        # Ollama reports "qwen3:8b"; a config that omits the tag still means it.
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in installed and self.model not in installed:
            available = ", ".join(sorted(installed)) or "none"
            raise LlmUnavailableError(
                f"model {self.model!r} is not installed. Run 'ollama pull {self.model}'. "
                f"Installed: {available}"
            )
        return f"{self.model} ready at {self._host}"

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], GenerationStats]:
        """Generate one JSON object matching `schema`.

        The schema is passed to Ollama as a grammar constraint rather than being
        described in the prompt: an 8B model asked politely for JSON returns
        prose often enough to matter, and constrained decoding removes the
        failure entirely.
        """
        body = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            # Reasoning is off: on a 4 GB GPU it doubles the wait, and this is a
            # writing task with the facts already supplied.
            "think": False,
            "options": {
                "num_ctx": self._settings.llm_context_tokens,
                # Low but not zero: identical letters to different companies
                # read as a mail merge, which is what we are avoiding.
                "temperature": 0.4,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
        }

        started = time.monotonic()
        try:
            response = self._client.post("/api/generate", json=body)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LlmError(f"generation failed on {self.model}: {error}") from error
        elapsed = time.monotonic() - started

        payload = response.json()
        raw = strip_thinking(str(payload.get("response", "")))
        if not raw:
            raise LlmError(f"{self.model} returned an empty response")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LlmError(
                f"{self.model} returned text that is not JSON: {raw[:200]!r}"
            ) from error
        if not isinstance(parsed, dict):
            raise LlmError(f"{self.model} returned {type(parsed).__name__}, expected an object")

        stats = GenerationStats(
            model=self.model,
            prompt_tokens=int(payload.get("prompt_eval_count") or 0),
            output_tokens=int(payload.get("eval_count") or 0),
            duration_seconds=elapsed,
        )
        logger.info(
            "generated %d tokens in %.1fs (%.1f tok/s) on %s",
            stats.output_tokens,
            stats.duration_seconds,
            stats.tokens_per_second,
            stats.model,
        )
        return parsed, stats
