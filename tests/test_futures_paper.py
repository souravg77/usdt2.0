"""Futures AI paper trades: isolated-margin liquidation, the liquidation check at the fill, and short
twins' exits after a restart."""

import pandas as pd

from aitrader.backtest import engine
from aitrader.data.store import CandleStore
from aitrader.learn import PaperConfig, PaperTrader, Registry, wallet
from aitrader.learn.paper import _sig_dict
from aitrader.models import Side
from aitrader.strategies.base import Signal
from test_learn import fill_store

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def _run(tmp_path, bars, entry=100.0, stop=98.0, target=110.0, lev=20.0, side="buy"):
    rows = [(T0 + i * H, 100, 100.2, 99.8, 100, 1000) for i in range(300)]
    rows += [(T0 + (300 + j) * H, o, h, lo, c, 1000) for j, (o, h, lo, c) in enumerate(bars)]
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]))
    Registry(str(tmp_path / "reg.json")).save()
    st = str(tmp_path / "state.json")
    wallet.save(wallet.wallet_path(st, "futures"), 1000, now_ms=0)
    wallet.set_leverage(wallet.market_path(st), lev)
    tr = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], PaperConfig(), state_path=st)
    sig = Signal(ts=T0 + 300 * H, strategy="x", exchange="bybit", symbol="BTC/USDT", timeframe="1h",
                 side=Side.SELL if side == "sell" else Side.BUY, entry=entry, stop=stop, target=target,
                 risk_pct=0.01, reason="t", features={})
    key = "x|bybit|BTC/USDT|1h" + ("|futures" if side == "buy" else "")
    tr.state["open"][key] = {"entry_id": "x", "signal": _sig_dict(sig), "situation": None, "teacher": None,
                             "size_mult": 1.0, "filled": False, "ai_prob": None, "ai_version": None,
                             "regime": None, "perp": True, "split_tp": False, "maker_entry": False}
    for j in range(len(bars)):
        tr.tick(now_ms=T0 + (301 + j) * H)
    store.close()
    return tr


def test_a_gap_past_liquidation_loses_the_margin_not_more(tmp_path):
    tr = _run(tmp_path, [(100, 100.5, 99.9, 100.2), (90, 90.5, 89, 90)])  # opens at 90: past the stop
    [t] = tr.state["trades"]
    margin = t["units"] * t["entry"] / 20
    assert t["exit_reason"] == "liquidated" and abs(t["pnl_usdt"] + margin) < 1e-6
    assert t["exit"] < t["entry"] and abs(t["exit"] - t["entry"] * (1 - 1 / 20 + 0.005)) < 1e-6
    short = _run(tmp_path / "s", [(100, 100.1, 99.5, 99.8), (110, 111, 109, 110)],
                 entry=100.0, stop=102.0, target=90.0, side="sell")
    [s] = short.state["trades"]
    assert s["exit_reason"] == "liquidated" and abs(s["pnl_usdt"] + s["units"] * s["entry"] / 20) < 1e-6


def test_at_1x_the_same_gap_is_a_normal_stop(tmp_path):
    tr = _run(tmp_path, [(100, 100.5, 99.9, 100.2), (90, 90.5, 89, 90)], lev=1.0)
    [t] = tr.state["trades"]
    assert t["exit_reason"] == "stop_gap" and t["pnl_usdt"] < 0


def test_a_fill_that_puts_liquidation_past_the_stop_is_skipped(tmp_path):
    tr = _run(tmp_path, [(103, 103.5, 102.8, 103.2), (103, 104, 102, 103)])  # opens 3% above the signal
    assert not tr.state["trades"] and not tr.state["open"]  # without the check it would be open


def test_a_short_twins_exit_works_before_the_twin_registered_it():
    df = pd.DataFrame({"close": [100 - i * 0.5 for i in range(60)]})
    df["open"], df["high"], df["low"] = df["close"], df["close"] + 0.1, df["close"] - 0.1
    engine.CUSTOM_EXITS.pop("flipped:rsi>70", None)  # as after a restart
    got = engine._one_trailing_exit("flipped:rsi>70", df)
    assert got is not None and bool(got.iloc[-1])  # falling price: overbought on the flipped chart
