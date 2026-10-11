"""Live websocket price feed with reconnect (exponential backoff) and a stale-price heartbeat."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

TickHandler = Callable[[str, str, float, int], Awaitable[None] | None]


class LiveFeed:
    """Streams last-trade prices from one exchange via ccxt.pro ``watch_ticker``.

    ``on_tick(exchange, symbol, price, ts_ms)`` is called for every update. If no update arrives
    for ``stale_after`` seconds the connection is treated as dead and rebuilt, and ``on_stale``
    is called so the trading loop can stop opening trades on old prices.
    """

    def __init__(
        self,
        exchange: str,
        symbols: list[str],
        on_tick: TickHandler,
        on_stale: Callable[[str], None] | None = None,
        stale_after: float = 30.0,
        max_backoff: float = 60.0,
        client_factory: Callable[[], object] | None = None,
    ):
        self.exchange = exchange
        self.symbols = symbols
        self.on_tick = on_tick
        self.on_stale = on_stale
        self.stale_after = stale_after
        self.max_backoff = max_backoff
        self._factory = client_factory or self._default_factory
        self.last_update: dict[str, float] = {}
        self._stop = asyncio.Event()

    def _default_factory(self):
        import ccxt.pro as ccxtpro

        return getattr(ccxtpro, self.exchange)({"enableRateLimit": True})

    def stop(self) -> None:
        self._stop.set()

    def is_stale(self, symbol: str, now: float | None = None) -> bool:
        last = self.last_update.get(symbol)
        return last is None or (now or time.monotonic()) - last > self.stale_after

    async def run(self) -> None:
        backoff = min(1.0, self.max_backoff)
        while not self._stop.is_set():
            client = self._factory()
            try:
                tasks = [asyncio.create_task(self._watch(client, s)) for s in self.symbols]
                tasks.append(asyncio.create_task(self._heartbeat()))
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    if t is not stop_task and t.exception():
                        raise t.exception()
                backoff = min(1.0, self.max_backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, stale feed, exchange hiccups
                log.warning("%s feed error: %s; reconnecting in %.0fs", self.exchange, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
            finally:
                close = getattr(client, "close", None)
                if close:
                    try:
                        await close()
                    except Exception:
                        pass

    async def _watch(self, client, symbol: str) -> None:
        while not self._stop.is_set():
            ticker = await client.watch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price is None:
                continue
            self.last_update[symbol] = time.monotonic()
            ts = int(ticker.get("timestamp") or time.time() * 1000)
            res = self.on_tick(self.exchange, symbol, float(price), ts)
            if asyncio.iscoroutine(res):
                await res

    async def _heartbeat(self) -> None:
        start = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(min(5.0, self.stale_after / 3))
            now = time.monotonic()
            if now - start < self.stale_after:
                continue
            stale = [s for s in self.symbols if self.is_stale(s, now)]
            if stale:
                for s in stale:
                    if self.on_stale:
                        self.on_stale(s)
                raise ConnectionError(f"stale prices for {', '.join(stale)}")
