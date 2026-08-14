"""The registry of company ATS boards that arm 1 polls.

A board token is the only thing needed to read a company's open roles, but
there is no directory of them -- they are discovered one company at a time,
from careers-page URLs. So the registry is a plain JSON file that grows: arm 2
appends boards it discovers, and ``jobbot boards verify`` prunes the ones that
stop responding.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from jobbot.config import get_settings
from jobbot.connectors.ats import PROVIDERS, Board

logger = logging.getLogger("jobbot.connectors.boards")


class BoardRegistryError(ValueError):
    """The registry file is malformed."""


def registry_path() -> Path:
    return get_settings().boards_file


def load_boards(path: Path | None = None) -> tuple[Board, ...]:
    path = path or registry_path()
    if not path.exists():
        logger.warning("no board registry at %s; arm 1 has nothing to poll", path)
        return ()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BoardRegistryError(f"{path} is not valid JSON: {error}") from error

    entries = payload.get("boards") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise BoardRegistryError(f"{path} must contain a list of boards")

    boards: list[Board] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BoardRegistryError(f"{path} board #{index} is not an object")
        provider = str(entry.get("provider", "")).strip().lower()
        token = str(entry.get("token", "")).strip()
        if not provider or not token:
            raise BoardRegistryError(f"{path} board #{index} needs both provider and token")
        if provider not in PROVIDERS:
            raise BoardRegistryError(
                f"{path} board #{index}: unknown provider {provider!r}; "
                f"expected one of {sorted(PROVIDERS)}"
            )
        key = (provider, token.lower())
        if key in seen:
            logger.warning("duplicate board %s:%s in %s, keeping the first", provider, token, path)
            continue
        seen.add(key)
        boards.append(
            Board(
                provider=provider,
                token=token,
                name=str(entry.get("name") or token),
                domain=str(entry.get("domain") or ""),
                country=(str(entry["country"]) if entry.get("country") else None),
            )
        )
    return tuple(boards)


def save_boards(boards: Iterable[Board], path: Path | None = None) -> Path:
    """Write the registry back, sorted so diffs stay readable."""
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(boards, key=lambda board: (board.provider, board.token.lower()))
    payload = {
        "boards": [
            {
                "provider": board.provider,
                "token": board.token,
                "name": board.name,
                "domain": board.domain,
                "country": board.country,
            }
            for board in ordered
        ]
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def merge_boards(existing: Sequence[Board], discovered: Iterable[Board]) -> tuple[Board, ...]:
    """Add newly discovered boards without disturbing existing entries."""
    by_key = {(board.provider, board.token.lower()): board for board in existing}
    for board in discovered:
        by_key.setdefault((board.provider, board.token.lower()), board)
    return tuple(by_key.values())
