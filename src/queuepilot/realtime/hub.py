"""Per-replica Redis listener and bounded connection queues."""

import asyncio
import logging
import uuid
from collections import deque
from contextlib import suppress

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from queuepilot.outbox.dispatcher import CHANNEL, Event

logger = logging.getLogger(__name__)


class Connection:
    def __init__(self) -> None:
        self.tickets: set[uuid.UUID] = set()
        self.versions: dict[uuid.UUID, int] = {}
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=32)
        self.overflow = False
        self._recent_ids: deque[uuid.UUID] = deque()
        self._seen_ids: set[uuid.UUID] = set()

    def seen(self, event_id: uuid.UUID) -> bool:
        if event_id in self._seen_ids:
            return True
        if len(self._recent_ids) == 256:
            self._seen_ids.remove(self._recent_ids.popleft())
        self._recent_ids.append(event_id)
        self._seen_ids.add(event_id)
        return False


class Hub:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.connections: set[Connection] = set()
        self.open_count = 0
        self.ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def close(self) -> None:
        self.ready.clear()
        for connection in self.connections:
            connection.overflow = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self.connections.clear()

    def try_open(self, limit: int) -> bool:
        if self.open_count >= limit:
            return False
        self.open_count += 1
        return True

    def release(self) -> None:
        self.open_count -= 1

    async def _listen(self) -> None:
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.subscribe(CHANNEL)
                    self.ready.set()
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        try:
                            if len(message["data"]) > 2048:
                                raise ValueError
                            event = Event.model_validate_json(message["data"])
                        except (ValidationError, ValueError, TypeError):
                            logger.warning("Discarded invalid realtime event")
                            continue
                        self.broadcast(event)
            except (RedisError, OSError, TimeoutError):
                self.ready.clear()
                logger.warning("Realtime Redis listener unavailable")
                await asyncio.sleep(0.5)

    def broadcast(self, event: Event) -> None:
        for connection in tuple(self.connections):
            if event.ticket_id not in connection.tickets:
                continue
            try:
                connection.queue.put_nowait(event)
            except asyncio.QueueFull:
                connection.overflow = True
