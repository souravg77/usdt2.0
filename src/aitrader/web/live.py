"""Live market data for the dashboard's charts, straight from Bybit's public API.

One shared, keyless ccxt client serves every viewer. Answers are cached for about a second, so
however many browsers poll, Bybit sees at most one request per pair and timeframe per second
(far under its public rate limits). When Bybit can't be reached, the candles saved by the
``collector`` service are served instead, and the answer says so. With ``perp=True`` (the
dashboard's Futures mode) prices and candles are the USDT perpetual's, with its funding rate.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import EXCHANGE
from ..data.store import CandleStore

log = logging.getLogger(__name__)

CACHE_S = 1.0
LIMIT = 500  # enough history for the 200-bar EMA on the chart


class LiveMarket:
    def __init__(self, db_path: str, exchange: str = EXCHANGE, client=None, clock=time.monotonic,
                 perp: bool = False):
        self.db_path, self.exchange, self.perp = db_path, exchange, perp
        self._client, self._clock = client, clock
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    def client(self):
        if self._client is None:
            import ccxt.async_support as ccxt_async

            # the charts are spot only: loading just the spot markets keeps the first request fast
            # (Bybit's full list adds perpetuals and options, several slow calls)
            kind = ("swap", "linear") if self.perp else ("spot", "spot")
            self._client = getattr(ccxt_async, self.exchange)(
                {"enableRateLimit": True,
                 "options": {"defaultType": kind[0], "fetchMarkets": {"types": [kind[1]]}}})
        return self._client

    async def _cached(self, key: tuple, fetch) -> dict:
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_S:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # one request per key at a time; the others reuse its answer
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < CACHE_S:
                return hit[1]
            value = await fetch()
            self._cache[key] = (self._clock(), value)
            return value

    async def candles(self, symbol: str, timeframe: str, limit: int = LIMIT) -> dict:
        async def fetch():
            try:
                rows = await asyncio.wait_for(
                    self.client().fetch_ohlcv(self._name(symbol), timeframe, limit=limit), timeout=8)
                return {"source": "live", "candles": [r[:6] for r in rows]}
            except Exception as exc:
                return {"source": "database", "note": _why(exc),
                        "candles": self._saved(symbol, timeframe, limit)}
        return await self._cached(("c", symbol, timeframe, limit), fetch)

    async def tickers(self, symbols: list[str]) -> dict:
        async def fetch():
            try:
                # every spot ticker in one call, then ours picked out: asking for a list makes ccxt
                # refuse the whole call when a single pair isn't listed, and the cards then froze
                # on the collector's hourly candles
                raw = await asyncio.wait_for(self.client().fetch_tickers(), timeout=8)
                rows = [{"symbol": s, "price": t.get("last"), "change": t.get("percentage"),
                         "high": t.get("high"), "low": t.get("low"), "volume": t.get("quoteVolume"),
                         **({"funding": _funding(t)} if self.perp else {})}
                        for s in symbols if (t := raw.get(self._name(s)))]
                if not rows:
                    raise LookupError("none of the pairs came back")
                return {"source": "live", "tickers": rows}
            except Exception as exc:
                log.warning("live tickers failed, using saved prices: %s: %s", type(exc).__name__, exc)
                return {"source": "database", "note": _why(exc), "tickers": self._saved_tickers(symbols)}
        return await self._cached(("t", tuple(symbols)), fetch)

    def _name(self, symbol: str) -> str:
        """The ccxt market name: BTC/USDT, or BTC/USDT:USDT for the perpetual."""
        return f"{symbol}:{symbol.split('/')[-1]}" if self.perp and ":" not in symbol else symbol

    # ---- fallback: the collector's candles --------------------------------------------------

    def _saved(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        try:
            store = CandleStore(self.db_path)
        except Exception:
            return []
        try:
            rows = store.load(self.exchange, symbol, timeframe)[-limit:]
        finally:
            store.close()
        return [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in rows]

    def _saved_tickers(self, symbols: list[str]) -> list[dict]:
        out = []
        for s in symbols:
            day = self._saved(s, "1h", 25)
            if day:
                base = day[0][1]
                out.append({"symbol": s, "price": day[-1][4],
                            "change": (day[-1][4] / base - 1) * 100 if base else None,
                            "high": max(r[2] for r in day), "low": min(r[3] for r in day), "volume": None})
        return out

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()


def _funding(t: dict) -> float | None:
    """The perpetual's current funding rate (per 8h, as a fraction) from Bybit's ticker."""
    for raw in ((t.get("info") or {}).get("fundingRate"), t.get("fundingRate")):
        try:
            if raw not in (None, ""):
                return float(raw)
        except (TypeError, ValueError):
            pass
    return None


def _why(exc: Exception) -> str:
    return f"Bybit not reachable right now ({type(exc).__name__}); showing saved prices"
