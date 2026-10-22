"""Paginated historical candle download: fills in older history before the first stored candle,
fills holes inside the requested window (a page the exchange skipped, the server being down), then
resumes from the last one. The newest stored candle is fetched once more, in case the exchange
hadn't finished it when it was first read."""

from __future__ import annotations

import logging
import time

from ..exchanges.base import ExchangeAdapter
from ..models import TIMEFRAME_MS
from .store import CandleStore

log = logging.getLogger(__name__)

# Per-request page size each exchange accepts (Bybit's kline endpoint returns up to 1000 bars).
PAGE_LIMIT = {"bybit": 1000}
MAX_HOLES = 10  # hole ranges filled per call; the rest wait for the next call
RETRY_HOLE_S = 6 * 3600  # a hole the exchange had no candles for is asked again after this
_tried: dict[tuple, float] = {}  # (exchange, symbol, timeframe, hole start) -> when it was last asked


async def download(
    adapter: ExchangeAdapter,
    store: CandleStore,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    drop_open_candle: bool = True,
) -> int:
    until_ms = until_ms or int(time.time() * 1000)
    first = store.first_ts(adapter.name, symbol, timeframe)
    last = store.last_ts(adapter.name, symbol, timeframe)
    total = 0
    older = (adapter.name, symbol, timeframe, "older", first)
    if first is not None and since_ms < first and time.time() - _tried.get(older, 0.0) >= RETRY_HOLE_S:
        # older history than what is stored (e.g. the collector keeps only recent weeks); a coin
        # listed after ``since_ms`` has none, so that is asked again only every few hours
        _tried[older] = time.time()
        total += await _fetch(adapter, store, symbol, timeframe, since_ms, min(first, until_ms),
                              drop_open_candle)
    if first is not None and last is not None:
        for start, end in _holes(store, adapter.name, symbol, timeframe, max(since_ms, first), min(last, until_ms)):
            n = await _fetch(adapter, store, symbol, timeframe, start, end, drop_open_candle)
            if n:
                log.info("%s %s %s: filled %d missing candles from %d", adapter.name, symbol, timeframe, n, start)
            total += n
    # from the newest stored candle (fetched again: it may have been read before it was final)
    cursor = max(since_ms, last) if last is not None else since_ms
    return total + await _fetch(adapter, store, symbol, timeframe, cursor, until_ms, drop_open_candle,
                                known={last} if last is not None else set())


def closed_since(timeframe: str, since_ms: int, now_ms: int) -> bool:
    """True when a candle of this timeframe closed after ``since_ms`` and by ``now_ms``."""
    step = TIMEFRAME_MS[timeframe]
    return now_ms // step > since_ms // step


async def top_up(adapter: ExchangeAdapter, store: CandleStore, symbol: str, timeframe: str,
                 drop_open_candle: bool = True) -> int | None:
    """Only the candles since the newest stored one (fetched again, as ``download`` does): the
    cheap first step of a collector pass, so every chart has its just-closed candle within a minute
    or so, however long the history and hole filling after it takes. None when nothing is stored
    yet (``download`` fetches the first history)."""
    last = store.last_ts(adapter.name, symbol, timeframe)
    if last is None:
        return None
    return await _fetch(adapter, store, symbol, timeframe, last, int(time.time() * 1000), drop_open_candle,
                        known={last})


async def deepen(adapter: ExchangeAdapter, store: CandleStore, symbols, timeframes, start_ms: int,
                 budget_s: float = 45.0, chunk_bars: int = 10_000) -> int:
    """Walk each pair's stored history back towards ``start_ms``, one chunk (``chunk_bars``
    candles) per pair and timeframe at a time, until ``budget_s`` seconds are used: Spot AI keeps
    its pairs' complete history without holding up the live top-up (later passes carry on where
    this one stopped). A coin listed after ``start_ms`` has no older candles; that is asked again
    only every few hours (``download``'s older-history rule). Returns how many candles were added."""
    began, total = time.monotonic(), 0
    for sym in symbols:
        for tf in timeframes:
            if time.monotonic() - began > budget_s:
                return total
            first = store.first_ts(adapter.name, sym, tf)
            if first is None or first <= start_ms + TIMEFRAME_MS[tf]:
                continue  # nothing stored yet (the top-up comes first), or already complete
            since = max(start_ms, first - chunk_bars * TIMEFRAME_MS[tf])
            n = await download(adapter, store, sym, tf, since, first)
            if n:
                log.info("%s %s %s: %d older candles (history back to %d)", adapter.name, sym, tf, n,
                         store.first_ts(adapter.name, sym, tf))
            total += n
    return total


def _holes(store, exchange, symbol, timeframe, start, end) -> list[tuple[int, int]]:
    """Ranges ``[from, to)`` of missing candles from ``start`` to ``end``, leaving out ranges asked
    for recently that the exchange had nothing for. The edges count too: a window that lies wholly
    inside a gap (candles stored before it and after it, none in it) is one hole, not none."""
    step = TIMEFRAME_MS[timeframe]
    stamps = store.timestamps(exchange, symbol, timeframe, start, end)
    if not stamps or stamps[0] > start:
        stamps.insert(0, start - step)
    if stamps[-1] < end:
        stamps.append(end)
    now, out = time.time(), []
    for a, b in zip(stamps, stamps[1:]):
        if b - a > step:
            key = (exchange, symbol, timeframe, a + step)
            if now - _tried.get(key, 0.0) < RETRY_HOLE_S:
                continue
            _tried[key] = now
            out.append((a + step, b))
            if len(out) >= MAX_HOLES:
                break
    return out


async def _fetch(adapter, store, symbol, timeframe, cursor, until_ms, drop_open_candle,
                 known: set | None = None) -> int:
    """Store every closed candle in ``[cursor, until_ms)``; returns how many were new
    (``known``: times already stored, fetched again only to refresh them)."""
    step = TIMEFRAME_MS[timeframe]
    limit = PAGE_LIMIT.get(adapter.name, 500)
    total = 0
    while cursor < until_ms:
        page = await adapter.get_candles(symbol, timeframe, since=cursor, limit=limit)
        page = [c for c in page if c.ts >= cursor and c.ts < until_ms]
        if drop_open_candle:
            now = int(time.time() * 1000)
            page = [c for c in page if c.ts + step <= now]
        if not page:
            break
        store.upsert(page)
        total += sum(1 for c in page if c.ts not in (known or ()))
        nxt = page[-1].ts + step
        if nxt <= cursor:
            break
        cursor = nxt
        log.info("%s %s %s: stored %d (up to %d)", adapter.name, symbol, timeframe, total, cursor)
    return total
