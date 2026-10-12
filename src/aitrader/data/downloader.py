"""Paginated historical candle download: fills in older history before the first stored candle,
then resumes from the last one."""

from __future__ import annotations

import logging
import time

from ..exchanges.base import ExchangeAdapter
from ..models import TIMEFRAME_MS
from .store import CandleStore

log = logging.getLogger(__name__)

# Per-request page size each exchange accepts (Bybit's kline endpoint returns up to 1000 bars).
PAGE_LIMIT = {"bybit": 1000}


async def download(
    adapter: ExchangeAdapter,
    store: CandleStore,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    drop_open_candle: bool = True,
) -> int:
    step = TIMEFRAME_MS[timeframe]
    until_ms = until_ms or int(time.time() * 1000)
    first = store.first_ts(adapter.name, symbol, timeframe)
    last = store.last_ts(adapter.name, symbol, timeframe)
    total = 0
    if first is not None and since_ms < first:
        # older history than what is stored (e.g. the collector keeps only recent weeks)
        total += await _fetch(adapter, store, symbol, timeframe, since_ms, min(first, until_ms),
                              drop_open_candle)
    cursor = max(since_ms, last + step) if last is not None else since_ms
    return total + await _fetch(adapter, store, symbol, timeframe, cursor, until_ms, drop_open_candle)


async def _fetch(adapter, store, symbol, timeframe, cursor, until_ms, drop_open_candle) -> int:
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
        total += store.upsert(page)
        nxt = page[-1].ts + step
        if nxt <= cursor:
            break
        cursor = nxt
        log.info("%s %s %s: stored %d (up to %d)", adapter.name, symbol, timeframe, total, cursor)
    return total
