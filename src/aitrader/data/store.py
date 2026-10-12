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

# Market-context series (funding, open interest, global market cap...): see data/context.py.
CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context (
    exchange TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    metric   TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    value    REAL NOT NULL,
    PRIMARY KEY (exchange, symbol, metric, ts)
) WITHOUT ROWID;
"""


# Schema changes after the first release go here, in order: (version, SQL). ``migrate`` applies
# the ones newer than the database's ``PRAGMA user_version``. Never edit a released entry.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA),  # the original candles table
    (2, CONTEXT_SCHEMA),  # market context: funding, open interest, global market data
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


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

    def first_ts(self, exchange: str, symbol: str, timeframe: str) -> int | None:
        row = self.conn.execute(
            "SELECT MIN(ts) FROM candles WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe),
        ).fetchone()
        return row[0]

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

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> list[int]:
        """Bring the database to ``SCHEMA_VERSION``. Returns the versions applied."""
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current} is newer than this code (v{SCHEMA_VERSION}); "
                               "update the code instead of downgrading the database")
        applied = []
        for version, sql in MIGRATIONS:
            if version > current:
                with self.conn:
                    self.conn.executescript(sql)
                    self.conn.execute(f"PRAGMA user_version = {int(version)}")
                applied.append(version)
        return applied

    def backup(self, path: str) -> None:
        """Consistent copy of the database, safe while other services write to it."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        dest = sqlite3.connect(path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        self.conn.close()
