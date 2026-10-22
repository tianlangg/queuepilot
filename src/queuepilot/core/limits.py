"""Bounded per-process request admission for sensitive HTTP routes."""

import asyncio
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 10_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._max_keys = max_keys

    async def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        async with self._lock:
            if len(self._hits) >= self._max_keys and key not in self._hits:
                self._hits = {
                    name: hits
                    for name, hits in self._hits.items()
                    if hits and hits[-1] > moment - 60
                }
                if len(self._hits) >= self._max_keys:
                    return False
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= moment - 60:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(moment)
            return True
