"""Audit logging.

Every meaningful state change is recorded twice: once to the structured Python
logger for live operation, once to the ``events`` table for after-the-fact
questions like "why did we contact this company" or "what did we actually send".
Recording an event must never be the reason a batch fails, so write errors are
logged and swallowed here — this is the one place where suppressing an
exception is the correct behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session

from jobbot.models import Event

logger = logging.getLogger("jobbot.events")


def record(
    session: Session,
    *,
    entity_type: str,
    entity_id: int | None,
    event: str,
    **payload: Any,
) -> Event | None:
    """Append an audit event. Returns None if the event could not be stored."""
    entry = Event(
        entity_type=entity_type,
        entity_id=entity_id,
        event=event,
        payload=payload,
    )
    logger.info(
        "%s.%s id=%s %s",
        entity_type,
        event,
        entity_id,
        payload if payload else "",
    )
    try:
        session.add(entry)
        session.flush()
    except Exception:
        logger.exception("failed to persist audit event %s.%s", entity_type, event)
        return None
    return entry
