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
    if first is not None and since_ms < first:
        # older history than what is stored (e.g. the collector keeps only recent weeks)
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


def _holes(store, exchange, symbol, timeframe, start, end) -> list[tuple[int, int]]:
    """Ranges ``[from, to)`` of missing candles between ``start`` and ``end`` (both stored),
    leaving out ranges asked for recently that the exchange had nothing for."""
    step = TIMEFRAME_MS[timeframe]
    stamps = store.timestamps(exchange, symbol, timeframe, start, end)
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
