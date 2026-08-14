"""Logging configuration.

Console output goes through Rich so long batch runs stay readable. Windows
consoles default to a legacy code page, so log messages must stay ASCII-only —
no symbols, no emoji — or the handler raises UnicodeEncodeError mid-run.
"""

from __future__ import annotations

import logging

from rich.logging import RichHandler

_configured = False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        logging.getLogger().setLevel(level)
        return

    handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        markup=False,
        log_time_format="%H:%M:%S",
    )
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
        force=True,
    )
    # These are noisy at DEBUG and never tell us anything we want.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
