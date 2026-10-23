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
from ..models import TIMEFRAME_MS

log = logging.getLogger(__name__)

CACHE_S = 1.0
LIMIT = 500  # enough history for the 200-bar EMA on the chart
RELOAD_S = 300.0  # reload Bybit's market list for an unknown coin at most this often
MAX_SINGLE = 20  # pairs asked for one by one when the all-tickers answer lacks them
HISTORY_LIMIT = 1000  # older candles per request when the chart is scrolled back (Bybit's page size)


class LiveMarket:
    def __init__(self, db_path: str, exchange: str = EXCHANGE, client=None, clock=time.monotonic,
                 perp: bool = False):
        self.db_path, self.exchange, self.perp = db_path, exchange, perp
        self._client, self._clock = client, clock
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._reloaded = -RELOAD_S  # when the market list was last reloaded for a new coin

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

    async def history(self, symbol: str, timeframe: str, before: int, step_ms: int, limit: int = HISTORY_LIMIT) -> dict:
        """Up to ``limit`` closed candles that opened before ``before`` (ms), oldest first, for
        scrolling the chart back in time; the collector's saved candles when Bybit can't be reached.
        An empty list means there is no older history."""
        limit = max(1, min(int(limit), HISTORY_LIMIT))
        since = before - limit * step_ms

        async def fetch():
            try:
                rows = await asyncio.wait_for(
                    self.client().fetch_ohlcv(self._name(symbol), timeframe, since=since, limit=limit), timeout=10)
                return {"source": "live", "candles": [r[:6] for r in rows if since <= r[0] < before]}
            except Exception as exc:
                return {"source": "database", "note": _why(exc),
                        "candles": [r for r in self._saved_range(symbol, timeframe, since, before)]}
        return await self._cached(("h", symbol, timeframe, before, limit), fetch)

    def _saved_range(self, symbol: str, timeframe: str, start: int, end: int) -> list[list]:
        try:
            store = CandleStore(self.db_path)
        except Exception:
            return []
        try:
            rows = [c for c in store.load(self.exchange, symbol, timeframe, start) if c.ts < end]
        finally:
            store.close()
        return [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in rows]

    async def tickers(self, symbols: list[str]) -> dict:
        async def fetch():
            try:
                # every spot ticker in one call, then ours picked out: asking for a list makes ccxt
                # refuse the whole call when a single pair isn't listed, and the cards then froze
                # on the collector's hourly candles
                raw = await asyncio.wait_for(self.client().fetch_tickers(), timeout=8)
                missing = [s for s in symbols if self._name(s) not in raw]
                if missing:  # most often a coin listed after the market list was loaded (a new top gainer)
                    raw = {**raw, **await self._fetch_missing(missing)}
                rows = [{"symbol": s, "price": t.get("last"), "change": t.get("percentage"),
                         "high": t.get("high"), "low": t.get("low"), "volume": t.get("quoteVolume"),
                         **({"funding": _funding(t)} if self.perp else {})}
                        for s in symbols if (t := raw.get(self._name(s)))]
                if not rows:
                    raise LookupError("none of the pairs came back")
                got = {r["symbol"] for r in rows}
                return {"source": "live", "tickers": rows + self._saved_tickers([s for s in symbols if s not in got])}
            except Exception as exc:
                log.warning("live tickers failed, using saved prices: %s: %s", type(exc).__name__, exc)
                return {"source": "database", "note": _why(exc), "tickers": self._saved_tickers(symbols)}
        return await self._cached(("t", tuple(symbols)), fetch)

    async def _fetch_missing(self, symbols: list[str]) -> dict:
        """Tickers of pairs Bybit's all-tickers answer didn't name: the market list is reloaded (at
        most every few minutes) so newly listed coins are known, then each is asked for by itself."""
        client = self.client()
        if self._clock() - self._reloaded > RELOAD_S and any(self._name(s) not in (client.markets or {}) for s in symbols):
            self._reloaded = self._clock()
            try:
                await asyncio.wait_for(client.load_markets(True), timeout=15)
            except Exception as exc:
                log.warning("reloading Bybit's market list failed: %s", exc)
        out = {}
        for s in symbols[:MAX_SINGLE]:
            try:
                out[self._name(s)] = await asyncio.wait_for(client.fetch_ticker(self._name(s)), timeout=5)
            except Exception as exc:
                log.info("no live ticker for %s: %s", s, exc)
        return out

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
            # only the recent end: a pair's full history (years of 1-minute candles) is slow to read
            step = TIMEFRAME_MS.get(timeframe)
            since = int(time.time() * 1000) - 4 * limit * step if step else None
            rows = store.load(self.exchange, symbol, timeframe, since)[-limit:]
        finally:
            store.close()
        return [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in rows]

    def _saved_tickers(self, symbols: list[str]) -> list[dict]:
        """The collector's newest saved prices: the last close of the shortest timeframe it keeps
        for the pair (1m when it can), and the 24-hour change from its hourly candles."""
        out = []
        for s in symbols:
            day = self._saved(s, "1h", 25)
            last = next((rows[-1] for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
                         if (rows := self._saved(s, tf, 1))), None)
            if not last:
                continue
            base = day[0][1] if day else None
            out.append({"symbol": s, "price": last[4],
                        "change": (last[4] / base - 1) * 100 if base else None,
                        "high": max(r[2] for r in day) if day else None,
                        "low": min(r[3] for r in day) if day else None, "volume": None})
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
