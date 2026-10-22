"""Bounded queues and strict event envelopes."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from queuepilot.core.config import Settings
from queuepilot.outbox.dispatcher import Event
from queuepilot.realtime.hub import Connection, Hub


def test_slow_connection_requires_resync() -> None:
    hub = Hub(Redis.from_url("redis://127.0.0.1:1/0"))
    connection = Connection()
    ticket_id = uuid.uuid4()
    connection.tickets.add(ticket_id)
    hub.connections.add(connection)
    for version in range(1, 34):
        hub.broadcast(
            Event(
                event_id=uuid.uuid4(),
                ticket_id=ticket_id,
                ticket_version=version,
                event_type="commented",
                timestamp=datetime.now(UTC),
            )
        )
    assert connection.queue.qsize() == 32
    assert connection.overflow


def test_event_rejects_content_and_invalid_fields() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "event_id": str(uuid.uuid4()),
                "ticket_id": str(uuid.uuid4()),
                "ticket_version": 1,
                "event_type": "commented",
                "timestamp": datetime.now(UTC).isoformat(),
                "description": "secret",
            }
        )


def test_websocket_origins_require_explicit_hosts() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="test", websocket_origins="*")
