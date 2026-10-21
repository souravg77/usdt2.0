import asyncio
import time

import pytest

from aitrader.config import Settings
from aitrader.data.downloader import download
from aitrader.data.feeds import LiveFeed
from aitrader.data.quality import check_candles
from aitrader.data.store import CandleStore
from aitrader.exchanges import BybitAdapter
from aitrader.models import Candle
from aitrader.throttle import Throttle


def c(ts, o=1.0, h=2.0, low=0.5, cl=1.5, v=1.0):
    return Candle("bybit", "BTC/USDT", "1m", ts, o, h, low, cl, v)


def test_quality_clean_series():
    rep = check_candles([c(i * 60_000) for i in range(10)], "1m")
    assert rep.ok, rep.as_text()


def test_quality_finds_problems():
    candles = [c(0), c(60_000), c(60_000), c(240_000), c(250_000), c(300_000, h=0.1)]
    rep = check_candles(candles, "1m")
    assert rep.duplicates == [60_000]
    assert rep.gaps == [(60_000, 2)]
    assert rep.misaligned == [250_000]
    assert rep.bad_bars == [300_000]
    assert not rep.ok


def test_store_upsert_is_idempotent(tmp_path):
    store = CandleStore(str(tmp_path / "x.db"))
    store.upsert([c(0), c(60_000)])
    store.upsert([c(60_000, cl=1.9), c(120_000)])
    rows = store.load("bybit", "BTC/USDT", "1m")
    assert [r.ts for r in rows] == [0, 60_000, 120_000]
    assert rows[1].close == 1.9
    assert store.last_ts("bybit", "BTC/USDT", "1m") == 120_000


async def test_download_paginates_and_resumes(fake_ccxt):
    s = Settings()
    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(25)]
    adapter = BybitAdapter(s, client=fake_ccxt(raw), throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    import aitrader.data.downloader as dl

    dl.PAGE_LIMIT["bybit"] = 10
    try:
        n = await download(adapter, store, "BTC/USDT", "1m", 0, 25 * 60_000)
        assert n == 25
        assert await download(adapter, store, "BTC/USDT", "1m", 0, 25 * 60_000) == 0
    finally:
        dl.PAGE_LIMIT["bybit"] = 1000
    assert check_candles(store.load("bybit", "BTC/USDT", "1m"), "1m").ok


async def test_download_fills_in_older_history(fake_ccxt):
    """The collector stores only recent candles; asking for years back must still fetch them."""
    s = Settings()
    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(40)]
    adapter = BybitAdapter(s, client=fake_ccxt(raw), throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    store.upsert([Candle("bybit", "BTC/USDT", "1m", i * 60_000, 1, 2, 0.5, 1.5, 1) for i in range(30, 35)])
    import aitrader.data.downloader as dl

    dl._tried.clear()
    n = await download(adapter, store, "BTC/USDT", "1m", 0, 40 * 60_000)
    assert n == 35  # 0-29 before the stored ones, 35-39 after
    rows = store.load("bybit", "BTC/USDT", "1m")
    assert [r.ts for r in rows] == [i * 60_000 for i in range(40)]
    assert store.first_ts("bybit", "BTC/USDT", "1m") == 0


async def test_throttle_limits_rate():
    clock = [0.0]
    t = Throttle(rate_per_sec=2, burst=1, clock=lambda: clock[0])
    await t.acquire()
    assert t.tokens == 0
    clock[0] = 0.5
    await t.acquire()  # refilled exactly one token


class FlakyClient:
    """First connection dies after one tick; the second keeps ticking."""

    instances = 0

    def __init__(self):
        FlakyClient.instances += 1
        self.n = FlakyClient.instances
        self.calls = 0

    async def watch_ticker(self, symbol):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.n == 1 and self.calls > 1:
            raise ConnectionError("socket closed")
        return {"last": 100.0 + self.calls, "timestamp": 1}

    async def close(self):
        pass


async def test_feed_reconnects_after_error():
    FlakyClient.instances = 0
    ticks = []
    feed = LiveFeed("bybit", ["BTC/USDT"], lambda *a: ticks.append(a),
                    client_factory=FlakyClient, max_backoff=0.01)
    feed_task = asyncio.create_task(feed.run())
    import aitrader.data.feeds as feeds

    orig = feeds.asyncio.sleep
    deadline = time.monotonic() + 5
    while FlakyClient.instances < 2 or len(ticks) < 3:
        assert time.monotonic() < deadline, "feed did not reconnect"
        await orig(0.05)
    feed.stop()
    await asyncio.wait_for(feed_task, 5)
    assert FlakyClient.instances >= 2
    assert not feed.is_stale("BTC/USDT")


def test_migrate_and_backup(tmp_path, monkeypatch):
    from aitrader.cli import main
    from aitrader.data import store as store_mod

    db = tmp_path / "a.db"
    s = store_mod.CandleStore(str(db))
    released = [v for v, _ in store_mod.MIGRATIONS]
    assert s.schema_version() == 0 and s.migrate() == released and s.migrate() == []
    s.upsert([Candle("bybit", "BTC/USDT", "1h", 0, 1, 2, 0.5, 1.5, 10)])
    nxt = store_mod.SCHEMA_VERSION + 1
    monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS + [
        (nxt, "CREATE TABLE IF NOT EXISTS extra (x INTEGER);")])
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", nxt)
    assert s.migrate() == [nxt] and s.schema_version() == nxt
    s.backup(str(tmp_path / "bk" / "a.db"))
    s.close()
    copy = store_mod.CandleStore(str(tmp_path / "bk" / "a.db"))
    assert len(copy.load("bybit", "BTC/USDT", "1h")) == 1 and copy.schema_version() == nxt
    copy.close()
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", nxt - 1)
    with pytest.raises(RuntimeError, match="newer"):
        store_mod.CandleStore(str(db)).migrate()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "b.db"))
    assert main(["--env-file", "none", "migrate"]) == 0
    assert main(["--env-file", "none", "backup", "data/backups/x"]) == 0
    assert (tmp_path / "data" / "backups" / "x" / "b.db").exists()


def test_backup_can_leave_the_candles_out(tmp_path):
    from aitrader.data import store as store_mod

    s = store_mod.CandleStore(str(tmp_path / "a.db"))
    s.migrate()
    s.upsert([Candle("bybit", "BTC/USDT", "1h", i * 3_600_000, 1, 2, 0.5, 1.5, 10) for i in range(500)])
    s.conn.execute("INSERT INTO context VALUES ('bybit', 'BTC/USDT', 'funding', 1, 0.0001)")
    s.conn.commit()
    s.backup(str(tmp_path / "bk" / "a.db"), candles=False)
    s.backup(str(tmp_path / "bk" / "a.db"), candles=False)  # an old file at the path is replaced
    copy = store_mod.CandleStore(str(tmp_path / "bk" / "a.db"))
    assert copy.load("bybit", "BTC/USDT", "1h") == [] and copy.schema_version() == s.schema_version()
    assert copy.conn.execute("SELECT COUNT(*) FROM context").fetchone()[0] == 1  # everything else is kept
    copy.upsert([Candle("bybit", "BTC/USDT", "1h", 0, 1, 2, 0.5, 1.5, 10)])  # and it works as a database
    assert len(copy.load("bybit", "BTC/USDT", "1h")) == 1
    copy.close()
    assert len(s.load("bybit", "BTC/USDT", "1h")) == 500  # the original is untouched
    s.close()


async def test_download_fills_holes_and_refreshes_the_newest_candle(fake_ccxt):
    import aitrader.data.downloader as dl

    s = Settings()
    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(30)]
    adapter = BybitAdapter(s, client=fake_ccxt(raw), throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    # stored with two holes, and the newest candle read before it was final
    have = [i for i in range(25) if i not in (5, 6, 7, 12)]
    store.upsert([Candle("bybit", "BTC/USDT", "1m", i * 60_000, 1, 2, 0.5, 1.0 if i == 24 else 1.5, 1)
                  for i in have])
    dl._tried.clear()
    n = await download(adapter, store, "BTC/USDT", "1m", 0, 30 * 60_000)
    assert n == 4 + 5  # the four missing, then 25-29
    rows = store.load("bybit", "BTC/USDT", "1m")
    assert [r.ts for r in rows] == [i * 60_000 for i in range(30)]
    assert rows[24].close == 1.5  # refreshed
    assert check_candles(rows, "1m").ok
    assert await download(adapter, store, "BTC/USDT", "1m", 0, 30 * 60_000) == 0


async def test_a_hole_the_exchange_has_nothing_for_is_not_asked_every_minute(fake_ccxt):
    import aitrader.data.downloader as dl

    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(10) if i != 4]  # Bybit has no bar 4
    calls = []
    client = fake_ccxt(raw)
    real = client.fetch_ohlcv

    async def counting(*a, **k):
        calls.append(k.get("since", a[2] if len(a) > 2 else None))
        return await real(*a, **k)

    client.fetch_ohlcv = counting
    adapter = BybitAdapter(Settings(), client=client, throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    dl._tried.clear()
    await download(adapter, store, "BTC/USDT", "1m", 0, 10 * 60_000)
    calls.clear()
    await download(adapter, store, "BTC/USDT", "1m", 0, 10 * 60_000)
    assert calls == [4 * 60_000, 9 * 60_000]  # the hole is asked for once, then the newest candle
    calls.clear()
    await download(adapter, store, "BTC/USDT", "1m", 0, 10 * 60_000)
    assert calls == [9 * 60_000]  # not again right away


async def test_download_fetches_an_old_month_page_by_page(fake_ccxt):
    """Bybit answers a kline request that has only a start with its newest bars, so a past month
    came back empty; each page must ask for its own start-to-end window."""
    import aitrader.data.downloader as dl

    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(5000)]
    adapter = BybitAdapter(Settings(), client=fake_ccxt(raw), throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    dl._tried.clear()
    n = await download(adapter, store, "BTC/USDT", "1m", 100 * 60_000, 2600 * 60_000)
    assert n == 2500 and store.count("bybit", "BTC/USDT", "1m", 0, 10**12) == 2500


async def test_history_before_a_new_listing_is_not_asked_every_minute(fake_ccxt):
    """The collector now asks for 250 candles on every timeframe; a coin listed later has fewer."""
    import aitrader.data.downloader as dl

    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 1] for i in range(20, 30)]  # listed at bar 20
    calls = []
    client = fake_ccxt(raw)
    real = client.fetch_ohlcv

    async def counting(*a, **k):
        calls.append(k.get("since", a[2] if len(a) > 2 else None))
        return await real(*a, **k)

    client.fetch_ohlcv = counting
    adapter = BybitAdapter(Settings(), client=client, throttle=Throttle(1000, 1000))
    store = CandleStore(":memory:")
    dl._tried.clear()
    await download(adapter, store, "NEW/USDT", "1m", 0, 30 * 60_000)
    calls.clear()
    await download(adapter, store, "NEW/USDT", "1m", 0, 30 * 60_000)
    assert 0 in calls  # the first time: older candles are asked for
    calls.clear()
    await download(adapter, store, "NEW/USDT", "1m", 0, 30 * 60_000)
    assert 0 not in calls  # not again right away
