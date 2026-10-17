import io
import json
import time

import numpy as np
import pandas as pd
import pytest

from aitrader.data import context as ctx_mod
from aitrader.data.context import (
    DAY_MS,
    HOUR_MS,
    ContextStore,
    attach_context,
    collect_context,
    has_context,
    perp_symbol,
)
from aitrader.data.store import SCHEMA_VERSION, CandleStore
from aitrader.models import Candle
from aitrader.strategies.context_filter import ContextFilter

NOW = 1_760_000_400_000 // HOUR_MS * HOUR_MS  # a round hour in Oct 2025


class FakeUsdm:
    """ccxt binanceusdm stand-in: funding every 8h, open interest and long/short every 1h."""

    def __init__(self, start, end, long_short=True):
        self.has = {"fetchLongShortRatioHistory": long_short}
        self.funding = [{"timestamp": t, "fundingRate": 0.0001} for t in range(start, end + 1, 8 * HOUR_MS)]
        self.oi = [{"timestamp": t, "openInterestAmount": 1000.0 + i} for i, t in
                   enumerate(range(start, end + 1, HOUR_MS))]
        self.ls = [{"timestamp": t, "longShortRatio": 1.5} for t in range(start, end + 1, HOUR_MS)]
        self.calls = []
        self.closed = False

    def _page(self, rows, since, limit):
        rows = [r for r in rows if since is None or r["timestamp"] >= since]
        return rows[:limit]

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        self.calls.append(("funding", symbol, since))
        return self._page(self.funding, since, limit)

    async def fetch_open_interest_history(self, symbol, timeframe="1h", since=None, limit=None):
        self.calls.append(("oi", symbol, since))
        return self._page(self.oi, since, limit)

    async def fetch_long_short_ratio_history(self, symbol, timeframe="1h", since=None, limit=None):
        return self._page(self.ls, since, limit)

    async def close(self):
        self.closed = True


def fake_urlopen(global_data=None, coins=None, fail=False):
    def urlopen(req, timeout=None):
        if fail:
            raise OSError("offline")
        url = req.full_url
        if url.endswith("/global"):
            body = {"data": global_data or {
                "updated_at": NOW // 1000,
                "market_cap_percentage": {"btc": 57.5},
                "total_market_cap": {"usd": 3.9e12},
                "market_cap_change_percentage_24h_usd": -2.5}}
        else:
            body = coins if coins is not None else [{"id": "tether", "market_cap": 1.6e11},
                                                    {"id": "usd-coin", "market_cap": 7.0e10}]
        return io.BytesIO(json.dumps(body).encode())
    return urlopen


@pytest.fixture
def ctx(tmp_path):
    c = ContextStore(str(tmp_path / "c.db"))
    yield c
    c.close()


def test_migration_v2_adds_context_table(tmp_path):
    s = CandleStore(str(tmp_path / "m.db"))
    assert not has_context(s)
    assert s.migrate() == [1, 2] and SCHEMA_VERSION == 2
    c = ContextStore(s)  # migrating again is a no-op
    assert s.schema_version() == 2 and not has_context(s)
    assert c.upsert([("binance", "BTC/USDT", "funding_rate", 5, 0.1), ("x", "y", "z", 1, float("nan"))]) == 1
    c.upsert([("binance", "BTC/USDT", "funding_rate", 5, 0.2)])  # same key replaces
    assert c.load("funding_rate", "binance", "BTC/USDT") == [(5, 0.2)] and has_context(s)
    s.close()


def test_perp_symbol():
    assert perp_symbol("BTC/USDT") == "BTC/USDT:USDT" and perp_symbol("ETH/USDT:USDT") == "ETH/USDT:USDT"


async def test_collect_tops_up_and_maps_symbols(ctx):
    client = FakeUsdm(NOW - 40 * DAY_MS, NOW)
    added = await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(), now_ms=NOW)
    assert added["funding_rate"] == 30 * 3 + 1
    assert added["open_interest"] == 29 * 24 + 1  # Binance keeps 30 days of OI history
    assert added["long_short_ratio"] == 29 * 24 + 1
    assert {c[1] for c in client.calls} == {"BTC/USDT:USDT"} and not client.closed
    assert ctx.value_at("btc_dominance", "global", "*") == (NOW, 57.5)
    assert ctx.value_at("total_mcap_change_24h", "global", "*")[1] == pytest.approx(-0.025)
    assert ctx.value_at("stable_mcap", "global", "*")[1] == pytest.approx(2.3e11)
    # second run only asks for what is new
    client.calls.clear()
    again = await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(), now_ms=NOW)
    assert again["funding_rate"] == 0 and again["open_interest"] == 0
    assert all(since > NOW - HOUR_MS for _, _, since in client.calls)


async def test_collect_skips_missing_long_short_and_offline_coingecko(ctx):
    client = FakeUsdm(NOW - 2 * DAY_MS, NOW, long_short=False)
    added = await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(fail=True), now_ms=NOW)
    assert added["long_short_ratio"] == 0 and added["total_mcap"] == 0 and added["funding_rate"] > 0


def candle_frame(n, step=HOUR_MS, start=NOW - 100 * HOUR_MS):
    ts = np.arange(n) * step + start
    return pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})


def test_attach_context_has_no_look_ahead(ctx):
    df = candle_frame(10)
    t0 = int(df["ts"].iloc[0])
    # a value stamped exactly at bar 2's close, and one a minute after bar 4's close
    ctx.upsert([("binance", "BTC/USDT", "funding_rate", t0 + 3 * HOUR_MS, 0.0003),
                ("binance", "BTC/USDT", "funding_rate", t0 + 5 * HOUR_MS + 60_000, 0.0009)])
    out = attach_context(df, ctx, "binance", "BTC/USDT", "1h")
    fr = out["funding_rate"].tolist()
    assert np.isnan(fr[0]) and np.isnan(fr[1])
    assert fr[2] == fr[3] == fr[4] == 0.0003  # visible from bar 2 (closes at its stamp)
    assert fr[5] == 0.0009 and len(out) == len(df) and "funding_rate" not in df
    assert out["open_interest"].isna().all()
    # timeframe inferred from the frame gives the same answer; other venues use Binance rows
    assert attach_context(df, ctx, "htx", "BTC/USDT")["funding_rate"].tolist()[2:6] == fr[2:6]


def test_attach_context_stops_carrying_stale_values(ctx):
    df = candle_frame(30)
    t0 = int(df["ts"].iloc[0])
    ctx.upsert([("binance", "BTC/USDT", "open_interest", t0 + HOUR_MS, 500.0)])
    oi = attach_context(df, ctx, "binance", "BTC/USDT", "1h")["open_interest"]
    assert oi.iloc[0] == oi.iloc[2] == 500.0 and np.isnan(oi.iloc[3])  # stale after 2h


def seed_candles(store, closes, step=HOUR_MS):
    start = NOW - len(closes) * step
    store.upsert([Candle("binance", "BTC/USDT", "1h", start + i * step, c, c, c, c, 1.0)
                  for i, c in enumerate(closes)])


def test_filter_defaults_allow_with_no_data(ctx):
    d = ContextFilter(ctx).decide("binance", "BTC/USDT", NOW)
    assert d.allow_entries and d.size_multiplier == 1.0 and len(d.reasons) == 3
    assert all("skipped" in r for r in d.reasons)


@pytest.mark.parametrize("rate, allow, size", [(0.0001, True, 1.0), (0.0006, True, 0.5), (0.0011, False, 0.0)])
def test_filter_funding(ctx, rate, allow, size):
    ctx.upsert([("binance", "BTC/USDT", "funding_rate", NOW - HOUR_MS, rate)])
    d = ContextFilter(ctx).decide("binance", "BTC/USDT", NOW)
    assert (d.allow_entries, d.size_multiplier) == (allow, size)
    # HTX markets read Binance's perpetual data
    assert ContextFilter(ctx).decide("htx", "BTC/USDT", NOW).size_multiplier == size


def test_filter_ignores_stale_funding(ctx):
    ctx.upsert([("binance", "BTC/USDT", "funding_rate", NOW - 17 * HOUR_MS, 0.01)])
    d = ContextFilter(ctx).decide("binance", "BTC/USDT", NOW)
    assert d.allow_entries and d.size_multiplier == 1.0 and "stale" in d.reasons[0]


def test_filter_open_interest_up_while_price_falls(ctx):
    ctx.upsert([("binance", "BTC/USDT", "open_interest", NOW - DAY_MS, 100.0),
                ("binance", "BTC/USDT", "open_interest", NOW, 120.0)])
    f = ContextFilter(ctx)
    assert f.decide("binance", "BTC/USDT", NOW, price_change_24h=-0.03).size_multiplier == 0.5
    assert f.decide("binance", "BTC/USDT", NOW, price_change_24h=0.03).size_multiplier == 1.0
    # price change read from stored 1h candles: 100 -> 95 over the last 24 closed bars
    seed_candles(ctx.candles, list(np.linspace(100, 95, 30)))
    d = f.decide("binance", "BTC/USDT", NOW)
    assert d.size_multiplier == 0.5 and any("open interest +20.0%" in r for r in d.reasons)
    seed_candles(ctx.candles, list(np.linspace(95, 100, 30)))
    assert f.decide("binance", "BTC/USDT", NOW).size_multiplier == 1.0


def test_filter_blocks_market_wide_selloff_and_rules_combine(ctx):
    ctx.upsert([("global", "*", "total_mcap_change_24h", NOW - HOUR_MS, -0.09)])
    d = ContextFilter(ctx).decide("binance", "BTC/USDT", NOW)
    assert not d.allow_entries and d.size_multiplier == 0.0 and "no new entries" in d.reasons[-1]
    assert ContextFilter(ctx).decide("binance", "BTC/USDT", NOW + 3 * HOUR_MS).allow_entries  # stale
    ctx.upsert([("global", "*", "total_mcap_change_24h", NOW, -0.02),
                ("binance", "BTC/USDT", "funding_rate", NOW, 0.0007),
                ("binance", "BTC/USDT", "open_interest", NOW - DAY_MS, 100.0),
                ("binance", "BTC/USDT", "open_interest", NOW, 130.0)])
    d = ContextFilter(ctx).decide("binance", "BTC/USDT", NOW, price_change_24h=-0.01)
    assert d.allow_entries and d.size_multiplier == 0.25 and len(d.reasons) == 2


def test_build_dataset_attaches_context(tmp_path, monkeypatch):
    from aitrader.ai import dataset

    s = CandleStore(str(tmp_path / "d.db"))
    seen = []
    monkeypatch.setattr(dataset, "attach_context", lambda df, c, ex, sym, tf: seen.append((ex, sym, tf)) or df)
    monkeypatch.setattr(dataset, "MIN_BARS", 1)
    seed_candles(s, [100.0] * 50)

    class Spec:
        timeframes = ["1h"]

    class NoSignals:
        spec = Spec()

        def generate(self, df, *a):
            return []

    dataset.build_dataset(s, [("x", NoSignals())], ["binance"], ["BTC/USDT"])
    assert seen == []  # no context rows: frame untouched
    ContextStore(s).upsert([("binance", "BTC/USDT", "funding_rate", NOW, 0.0001)])
    dataset.build_dataset(s, [("x", NoSignals())], ["binance"], ["BTC/USDT"])
    assert seen == [("binance", "BTC/USDT", "1h")]
    s.close()


def test_cli_context_collect_and_show(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "cli.db"))
    now = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    monkeypatch.setattr(ctx_mod, "make_client", lambda: FakeUsdm(now - 2 * DAY_MS, now))
    monkeypatch.setattr(ctx_mod.urllib.request, "urlopen", fake_urlopen())
    assert main(["--env-file", "none", "context", "collect", "--once", "--symbol", "BTC/USDT"]) == 0
    assert main(["--env-file", "none", "context", "show", "--symbol", "BTC/USDT"]) == 0
    out = capsys.readouterr().out
    assert "funding_rate" in out and "btc_dominance" in out and "now: entries" in out
