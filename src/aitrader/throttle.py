"""Async token-bucket throttle, layered on top of ccxt's own rate limiter as a safety margin."""

from __future__ import annotations

import asyncio
import time


class Throttle:
    def __init__(self, rate_per_sec: float, burst: int | None = None, clock=time.monotonic):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_sec)))
        self.tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                await asyncio.sleep((cost - self.tokens) / self.rate)


# Conservative defaults, well under each exchange's published REST limits.
# Bybit: 600 requests per 5 s per IP for market data, 20/s per key for spot orders.
DEFAULT_RATES = {"bybit": 8.0}
