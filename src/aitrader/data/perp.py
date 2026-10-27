"""Futures AI's own market data: Bybit USDT perpetual candles, kept apart from Spot AI's.

Futures AI never reads a spot price. Its candles come from the perpetual (``BTC/USDT:USDT``, Bybit
category ``linear``) and live in their own database (``futures_db_path``), saved under the pair's
name (``BTC/USDT``) so every strategy, backtest and page reads them the same way as spot candles.
Spot AI keeps the main database with spot candles only.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from ..models import Candle

_DATED = re.compile(r":USDT-(\d{2})(\d{2})(\d{2})$")


def futures_db_path(db_path: str, env=None) -> str:
    """Futures AI's candle database: ``AITRADER_FUTURES_DB_PATH``, else next to the spot one
    (data/aitrader.db -> data/aitrader_futures.db)."""
    env = os.environ if env is None else env
    if env.get("AITRADER_FUTURES_DB_PATH"):
        return env["AITRADER_FUTURES_DB_PATH"]
    stem, ext = os.path.splitext(db_path)
    return f"{stem}_futures{ext or '.db'}"


def db_for(db_path: str, market: str) -> str:
    """The candle database an engine reads: the futures one for "futures", else ``db_path``."""
    return futures_db_path(db_path) if market == "futures" else db_path


def perp_symbol(symbol: str) -> str:
    """The ccxt name of a pair's USDT perpetual: BTC/USDT -> BTC/USDT:USDT."""
    return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"


def is_dated(symbol: str) -> bool:
    """Whether ``symbol`` is a dated (expiring) Bybit USDT futures contract, named the ccxt way
    (``BTC/USDT:USDT-261030`` = BTCUSDT-30OCT26), rather than a perpetual."""
    return bool(_DATED.search(symbol or ""))


def expiry_ms(symbol: str) -> int | None:
    """When a dated contract is delivered (Bybit settles them at 08:00 UTC on the expiry day), or
    None for a perpetual."""
    m = _DATED.search(symbol or "")
    if not m:
        return None
    yy, mm, dd = (int(x) for x in m.groups())
    try:
        return int(datetime(2000 + yy, mm, dd, 8, tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def bybit_name(symbol: str) -> str:
    """How Bybit's app writes a dated contract (BTC/USDT:USDT-261030 -> BTCUSDT-30OCT26); other
    symbols are returned as they are."""
    m = _DATED.search(symbol or "")
    if not m:
        return symbol
    yy, mm, dd = m.groups()
    months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
    base = symbol.split("/")[0]
    return f"{base}USDT-{dd}{months[int(mm) - 1] if 1 <= int(mm) <= 12 else mm}{yy}"


class PerpCandles:
    """An exchange adapter seen through its USDT perpetuals: ``get_candles("BTC/USDT")`` returns
    the perpetual's candles, named after the pair (for ``data.downloader.download``)."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.name = adapter.name

    async def get_candles(self, symbol, timeframe, since=None, limit=500):
        rows = await self.adapter.get_candles(perp_symbol(symbol), timeframe, since=since, limit=limit)
        return [Candle(c.exchange, symbol, c.timeframe, c.ts, c.open, c.high, c.low, c.close, c.volume)
                for c in rows]

    async def close(self):
        await self.adapter.close()
