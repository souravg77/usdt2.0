"""Paginated historical candle download, resumable from the last stored candle."""

from __future__ import annotations

import logging
import time

from ..exchanges.base import ExchangeAdapter
from ..models import TIMEFRAME_MS
from .store import CandleStore

log = logging.getLogger(__name__)

# Per-request page size each exchange accepts. HTX returns at most 2000 bars and only
# recent history (it ignores `since` beyond its window), so HTX history is shorter.
PAGE_LIMIT = {"binance": 1000, "htx": 1000}


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
    last = store.last_ts(adapter.name, symbol, timeframe)
    cursor = max(since_ms, last + step) if last is not None else since_ms
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
