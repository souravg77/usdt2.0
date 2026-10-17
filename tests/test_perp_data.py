"""Futures AI reads only Bybit USDT perpetual data, from its own database; Spot AI only spot data
(Shawn: "Don't take anything for spot. Configure future separately, from the core")."""

import asyncio
import types

import pandas as pd

from aitrader.data.perp import PerpCandles, db_for, futures_db_path, perp_symbol
from aitrader.data.store import CandleStore
from aitrader.learn import PaperConfig, PaperTrader, Registry, wallet
from aitrader.learn.paper import _sig_dict
from aitrader.models import Candle, Side
from aitrader.strategies.base import Signal
from test_learn import fill_store

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def test_the_futures_database_sits_next_to_the_spot_one():
    assert futures_db_path("data/aitrader.db", env={}) == "data/aitrader_futures.db"
    assert futures_db_path("x.db", env={"AITRADER_FUTURES_DB_PATH": "/f.db"}) == "/f.db"
    assert db_for("data/aitrader.db", "spot") == "data/aitrader.db"
    assert perp_symbol("1000PEPE/USDT") == "1000PEPE/USDT:USDT" and perp_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_perp_candles_come_from_the_perpetual_under_the_pairs_name(tmp_path):
    from aitrader.data.downloader import download

    asked = []

    class Adapter:
        name = "bybit"

        async def get_candles(self, symbol, timeframe, since=None, limit=500):
            asked.append(symbol)
            if since >= T0 + 3 * H:
                return []
            return [Candle("bybit", symbol, timeframe, T0 + i * H, 1, 2, 0.5, 1.5, 10) for i in range(3)]

    store = CandleStore(str(tmp_path / "f.db"))
    n = asyncio.run(download(PerpCandles(Adapter()), store, "BTC/USDT", "1h", T0, T0 + 3 * H))
    assert n == 3 and set(asked) == {"BTC/USDT:USDT"}  # the perpetual is asked, never the spot pair
    assert [c.symbol for c in store.load("bybit", "BTC/USDT", "1h")] == ["BTC/USDT"] * 3
    store.close()


def test_download_market_futures_fills_only_the_futures_database(tmp_path, monkeypatch):
    from aitrader import cli
    from aitrader.config import Settings

    asked = []

    class Adapter:
        name = "bybit"

        async def get_candles(self, symbol, timeframe, since=None, limit=500):
            asked.append(symbol)
            return [] if since >= T0 + 2 * H else [Candle("bybit", symbol, timeframe, T0, 1, 2, 0.5, 1.5, 10)]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "make_adapter", lambda ex, s: Adapter())
    monkeypatch.delenv("AITRADER_FUTURES_DB_PATH", raising=False)
    s = Settings(db_path=str(tmp_path / "a.db"))
    args = types.SimpleNamespace(since="2023-11-14", until="2023-11-15", exchange=["bybit"], symbol=["SOL/USDT"],
                                 timeframe=["1h"], market="futures")
    asyncio.run(cli.cmd_download(s, args))
    assert set(asked) == {"SOL/USDT:USDT"}
    f, spot = CandleStore(str(tmp_path / "a_futures.db")), CandleStore(str(tmp_path / "a.db"))
    assert f.load("bybit", "SOL/USDT", "1h") and not spot.load("bybit", "SOL/USDT", "1h")
    f.close()
    spot.close()


def _stores(tmp_path):
    def make(path, price):
        rows = [(T0 + i * H, price, price * 1.002, price * 0.998, price, 1000) for i in range(305)]
        store = CandleStore(str(path))
        fill_store(store, pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]))
        return store

    return make(tmp_path / "spot.db", 100.0), make(tmp_path / "perp.db", 101.0)  # the perp trades 1% higher


def _position(sig, perp):
    return {"entry_id": "x", "signal": _sig_dict(sig), "situation": None, "teacher": None, "size_mult": 1.0,
            "filled": False, "ai_prob": None, "ai_version": None, "regime": None, "perp": perp,
            "split_tp": False, "maker_entry": False}


def test_each_engine_fills_and_marks_on_its_own_prices(tmp_path):
    spot, perp = _stores(tmp_path)
    Registry(str(tmp_path / "reg.json")).save()
    st = str(tmp_path / "state.json")
    wallet.save(wallet.wallet_path(st, "futures"), 1000, now_ms=0)
    tr = PaperTrader(spot, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], PaperConfig(), state_path=st,
                     futures_store=perp)
    sig = Signal(ts=T0 + 300 * H, strategy="x", exchange="bybit", symbol="BTC/USDT", timeframe="1h", side=Side.BUY,
                 entry=100.0, stop=95.0, target=130.0, risk_pct=0.01, reason="t", features={})
    key = "x|bybit|BTC/USDT|1h"
    tr.state["open"][key] = _position(sig, False)
    tr.state["open"][key + "|futures"] = _position(sig, True)
    tr.tick(now_ms=T0 + 303 * H)
    s, f = tr.state["open"][key], tr.state["open"][key + "|futures"]
    assert abs(s["entry"] - 100.0) < 0.2 and s["mark"] == 100.0  # Spot AI: spot candles (plus slippage)
    assert abs(f["entry"] - 101.0) < 0.2 and f["mark"] == 101.0  # Futures AI: the perpetual's candles only
    spot.close()
    perp.close()


def test_futures_ai_has_no_candles_without_the_perpetuals(tmp_path):
    """No perpetual candles yet: Futures AI waits instead of falling back to spot prices."""
    spot, _perp = _stores(tmp_path)
    empty = CandleStore(str(tmp_path / "empty.db"))
    Registry(str(tmp_path / "reg.json")).save()
    tr = PaperTrader(spot, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], PaperConfig(),
                     state_path=str(tmp_path / "s.json"), futures_store=empty)
    assert tr._frame("bybit", "BTC/USDT", "1h", T0, T0 + 300 * H, "futures") is None
    assert len(tr._frame("bybit", "BTC/USDT", "1h", T0, T0 + 300 * H, "spot")) == 300
    spot.close()
    empty.close()
