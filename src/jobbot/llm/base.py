"""What arm 3 asks a language model for, independent of who answers.

The model is the one component here with no guaranteed behaviour: it can return
prose when asked for JSON, invent a detail the posting never contained, or
quietly drift into a language the recipient does not read. So the contract is
narrow -- one call, one structured result -- and everything the result must
satisfy is checked afterwards by code, not trusted because the model was asked
nicely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LlmError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


class LlmUnavailableError(LlmError):
    """The model is not running or not pulled."""


@dataclass(frozen=True, slots=True)
class Draft:
    """One generated application, before any validation."""

    subject: str
    body: str
    cited_detail: str
    language: str

    @property
    def word_count(self) -> int:
        return len(self.body.split())


@dataclass(frozen=True, slots=True)
class GenerationStats:
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return self.output_tokens / self.duration_seconds


@runtime_checkable
class LlmClient(Protocol):
    """A local or remote model that can return a JSON object on demand."""

    model: str

    def health(self) -> str: ...

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], GenerationStats]: ...
