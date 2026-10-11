"""SQLite candle store. One table, primary key (exchange, symbol, timeframe, ts) so re-downloads upsert."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable

from ..models import Candle

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    exchange  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    open      REAL NOT NULL,
    high      REAL NOT NULL,
    low       REAL NOT NULL,
    close     REAL NOT NULL,
    volume    REAL NOT NULL,
    PRIMARY KEY (exchange, symbol, timeframe, ts)
) WITHOUT ROWID;
"""


class CandleStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def upsert(self, candles: Iterable[Candle]) -> int:
        rows = [
            (c.exchange, c.symbol, c.timeframe, c.ts, c.open, c.high, c.low, c.close, c.volume)
            for c in candles
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def load(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start: int | None = None,
        end: int | None = None,
    ) -> list[Candle]:
        q = "SELECT * FROM candles WHERE exchange=? AND symbol=? AND timeframe=?"
        args: list = [exchange, symbol, timeframe]
        if start is not None:
            q += " AND ts>=?"
            args.append(start)
        if end is not None:
            q += " AND ts<?"
            args.append(end)
        q += " ORDER BY ts"
        return [Candle(*row) for row in self.conn.execute(q, args)]

    def last_ts(self, exchange: str, symbol: str, timeframe: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) FROM candles WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe),
        ).fetchone()
        return row[0]

    def summary(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT exchange, symbol, timeframe, COUNT(*), MIN(ts), MAX(ts) FROM candles "
            "GROUP BY exchange, symbol, timeframe ORDER BY 1,2,3"
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
