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
        self.conn = sqlite3.connect(path, timeout=60)  # a download and a long job may write at once
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

    def timestamps(self, exchange: str, symbol: str, timeframe: str, start: int, end: int) -> list[int]:
        """Stored candle times in ``[start, end]``, oldest first."""
        return [r[0] for r in self.conn.execute(
            "SELECT ts FROM candles WHERE exchange=? AND symbol=? AND timeframe=? AND ts>=? AND ts<=? "
            "ORDER BY ts", (exchange, symbol, timeframe, start, end))]

    def count(self, exchange: str, symbol: str, timeframe: str, start: int, end: int) -> int:
        """How many candles are stored in ``[start, end)``."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND timeframe=? AND ts>=? AND ts<?",
            (exchange, symbol, timeframe, start, end)).fetchone()[0]

    def version(self) -> tuple[int, int]:
        """Changes whenever a candle is written, by this connection or another process: a cheap
        way to tell that nothing new arrived since the last look."""
        return self.conn.execute("PRAGMA data_version").fetchone()[0], self.conn.total_changes

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

    def backup(self, path: str, candles: bool = True) -> None:
        """Consistent copy of the database, safe while other services write to it. With
        ``candles=False`` the candles table is copied empty: price history can always be downloaded
        again, and it is nearly all of the file (about 2 GB a year for 20 pairs), so a daily
        backup without it is a few MB."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if candles:
            dest = sqlite3.connect(path)
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
            return
        if os.path.exists(path):
            os.remove(path)
        self.conn.execute("ATTACH DATABASE ? AS bk", (path,))
        try:
            with self.conn:
                for name, sql in self.conn.execute(
                        "SELECT name, sql FROM main.sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'").fetchall():
                    # (sqlite keeps the CREATE text without IF NOT EXISTS, so this is "CREATE TABLE name")
                    self.conn.execute(sql.replace("CREATE TABLE ", "CREATE TABLE bk.", 1))
                    if name != "candles":
                        self.conn.execute(f'INSERT INTO bk."{name}" SELECT * FROM main."{name}"')
                for (sql,) in self.conn.execute("SELECT sql FROM main.sqlite_master WHERE type = 'index' "
                                                "AND sql IS NOT NULL").fetchall():
                    self.conn.execute(sql.replace("CREATE INDEX ", "CREATE INDEX bk.", 1)
                                      .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX bk.", 1))
                version = self.conn.execute("PRAGMA main.user_version").fetchone()[0]
                self.conn.execute(f"PRAGMA bk.user_version = {int(version)}")
        finally:
            self.conn.execute("DETACH DATABASE bk")

    def close(self) -> None:
        self.conn.close()
