"""What every mail transport must provide.

The interesting logic — daily caps, per-company cooldown, pacing, plain-text
bodies, attachment limits — is transport-independent and lives elsewhere. A
transport only has to answer two questions: can you send right now, and please
send this. That keeps swapping providers a small change rather than a rewrite.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from jobbot.config import PROJECT_ROOT

# Providers reject very large payloads and inline base64 inflates by ~33%.
# A CV over this needs compressing, not a chunked upload path.
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024


class SendError(RuntimeError):
    """The message could not be sent."""


class NotConfiguredError(SendError):
    """Sending is not set up yet."""


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    content: bytes

    @classmethod
    def from_path(cls, path: str | Path) -> Attachment:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = PROJECT_ROOT / resolved
        if not resolved.exists():
            raise SendError(f"attachment not found: {resolved}")
        data = resolved.read_bytes()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise SendError(
                f"{resolved.name} is {len(data) / 1_048_576:.1f} MB; the limit is "
                f"{MAX_ATTACHMENT_BYTES / 1_048_576:.0f} MB"
            )
        return cls(name=resolved.name, content=data)

    @property
    def base64_content(self) -> str:
        return base64.b64encode(self.content).decode("ascii")


@dataclass(frozen=True, slots=True)
class SentMessage:
    to: str
    subject: str
    attachment_names: tuple[str, ...] = ()
    provider_id: str = ""


@runtime_checkable
class Sender(Protocol):
    """A mail transport."""

    def health(self) -> str: ...

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> SentMessage: ...
