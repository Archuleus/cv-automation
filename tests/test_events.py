from __future__ import annotations

import logging

from sqlmodel import Session, select

from jobbot import events
from jobbot.models import Company, Event


class TestRecord:
    def test_persists_event_with_payload(self, session: Session, company: Company):
        # Act
        events.record(
            session,
            entity_type="company",
            entity_id=company.id,
            event="discovered",
            source="greenhouse",
            score=91,
        )
        session.commit()

        # Assert
        stored = session.exec(select(Event)).one()
        assert stored.entity_type == "company"
        assert stored.event == "discovered"
        assert stored.payload == {"source": "greenhouse", "score": 91}

    def test_empty_payload_is_an_empty_dict(self, session: Session):
        events.record(session, entity_type="run", entity_id=None, event="started")
        session.commit()

        assert session.exec(select(Event)).one().payload == {}

    def test_audit_failure_does_not_propagate(self, session: Session, caplog):
        # Arrange -- a payload the JSON serialiser cannot handle
        class Unserialisable:
            pass

        # Act
        with caplog.at_level(logging.ERROR):
            result = events.record(
                session,
                entity_type="job",
                entity_id=1,
                event="scored",
                blob=Unserialisable(),
            )

        # Assert -- logging an event must never break the batch that logged it
        assert result is None
        assert "failed to persist audit event" in caplog.text
