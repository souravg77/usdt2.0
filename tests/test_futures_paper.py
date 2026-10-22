"""Futures AI paper trades: isolated-margin liquidation, the liquidation check at the fill, and short
twins' exits after a restart."""

import pandas as pd

from aitrader.backtest import engine
from aitrader.data.store import CandleStore
from aitrader.learn import PaperConfig, PaperTrader, Registry, guards, wallet
from aitrader.learn.paper import _sig_dict
from aitrader.models import Side
from aitrader.strategies.base import Signal
from aitrader.trade import futures
from test_learn import fill_store

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def _run(tmp_path, bars, entry=100.0, stop=98.0, target=110.0, lev=20.0, side="buy", amount=0.0, max_rr=0.0):
    rows = [(T0 + i * H, 100, 100.2, 99.8, 100, 1000) for i in range(300)]
    rows += [(T0 + (300 + j) * H, o, h, lo, c, 1000) for j, (o, h, lo, c) in enumerate(bars)]
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]))
    Registry(str(tmp_path / "reg.json")).save()
    st = str(tmp_path / "state.json")
    wallet.save(wallet.wallet_path(st, "futures"), 1000, now_ms=0)
    wallet.set_leverage(wallet.market_path(st), lev)
    # the strategy's own target unless a test sets the furthest take-profit (Futures AI starts at 2R)
    guards.save(guards.path_for(st), "futures", {"max_rr": max_rr, "trade_usdt": amount or None})
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


def test_a_stop_too_wide_for_the_leverage_trades_at_less(tmp_path):
    tr = _run(tmp_path, [(103, 103.5, 102.8, 103.2), (103, 104, 102, 103)])  # opens 3% above the signal
    [pos] = tr.state["open"].values()  # opened, not skipped
    assert 1 <= pos["leverage"] < 20
    assert futures.long_stop_ok(pos["entry"], 98.0, pos["leverage"])
    assert futures.long_liquidation_price(pos["entry"], pos["leverage"]) < 98.0


def test_a_fixed_amount_per_trade_is_the_margin(tmp_path):
    bars = [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100, 100.4)]
    auto = _run(tmp_path / "a", bars, lev=5.0)
    [a] = auto.state["open"].values()
    assert abs(a["units"] - 1000 * 0.01 / 2) < 0.01  # about 1% risk of 1000 over a 2 USDT stop
    fixed = _run(tmp_path / "f", bars, lev=5.0, amount=50)
    [f] = fixed.state["open"].values()
    assert abs(f["units"] * f["entry"] / f["leverage"] - 50) < 1e-6  # 50 USDT margin
    assert abs(f["units"] * f["entry"] - 250) < 1e-6  # a 250 USDT position at 5x
    big = _run(tmp_path / "b", bars, lev=1.0, amount=5000)  # more than the wallet: what is free
    [b] = big.state["open"].values()
    assert abs(b["units"] * b["entry"] - 1000) < 1e-6


def test_the_position_shows_its_leverage_margin_and_why_it_is_less(tmp_path):
    from aitrader.web.app import _lev_cell

    tr = _run(tmp_path, [(103, 103.5, 102.8, 103.2), (103, 104, 102, 103)])  # stop too wide for 20x
    [pos] = tr.state["open"].values()
    assert pos["lev_setting"] == 20 and pos["lev_why"] == "stop too wide for 20x"
    cell = _lev_cell(pos, "buy")
    assert f"{pos['leverage']:g}x" in cell and "setting 20x; stop too wide for 20x" in cell and "margin" in cell
    ok = _run(tmp_path / "ok", [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100, 100.4)], lev=5.0, amount=10)
    [o] = ok.state["open"].values()
    cell = _lev_cell(o, "buy")
    assert o["leverage"] == 5 and "lev_why" not in o
    assert "<b>5x</b>" in cell and "margin 10.00 × 5 = 50.00 USDT position" in cell and "setting" not in cell


def test_small_coins_are_liquidated_sooner_so_their_leverage_is_cut_sooner(tmp_path):
    # Bybit: maintenance margin = half the initial margin of the lowest tier (1 / the coin's maximum)
    assert futures.maintenance_rate(100) == 0.005 and futures.maintenance_rate(25) == 0.02
    assert futures.maintenance_rate(None) == futures.MAINTENANCE_MARGIN
    assert futures.long_liquidation_price(100, 20, 0.02) == 100 * (1 - 0.05 + 0.02)
    assert futures.safe_leverage(100, 98, False, 20, 0.02) < futures.safe_leverage(100, 98, False, 20)
    import json

    lp = tmp_path / "limits.json"
    lp.write_text(json.dumps({"generated": 1, "max_leverage": {"BTC/USDT": 25}}))
    tr = _run(tmp_path / "r", [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100, 100.4)], lev=20.0)
    tr.perp_limits_path = str(lp)
    assert tr._mmr("BTC/USDT") == 0.02 and tr._mmr("XRP/USDT") == 0.005
    [pos] = tr.state["open"].values()
    assert pos["mmr"] == 0.005  # the limits file of this run was not there yet: the BTC tier


def test_automatic_size_caps_the_margin_not_the_position(tmp_path):
    # a tight 0.2% stop: 1% risk of 1000 would be a 5,000 USDT position; at 20x its margin is 250,
    # within half the wallet, so it opens at that size (at 1x it is cut to half the wallet)
    bars = [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100.1, 100.4)]
    lev = _run(tmp_path / "l", bars, stop=99.8, lev=20.0)
    [p] = lev.state["open"].values()
    assert p["leverage"] == 20 and abs(p["units"] - 1000 * 0.01 / 0.2) < 1e-6
    one = _run(tmp_path / "o", bars, stop=99.8, lev=1.0)
    [o] = one.state["open"].values()
    assert abs(o["units"] * o["entry"] - 500) < 1e-6


def test_safe_leverage():
    # a 5% stop on a short (the APT/USDT 4h signal): 20x would put liquidation inside it
    assert not futures.short_stop_ok(0.8566, 0.899665, 20)
    lev = futures.safe_leverage(0.8566, 0.899665, True, 20)
    assert 9 <= lev < 10 and futures.short_stop_ok(0.8566, 0.899665, lev)
    assert futures.safe_leverage(100, 99.5, False, 20) == 20  # a tight stop keeps the setting
    assert futures.safe_leverage(100, 40, False, 20) == 0  # past liquidation even at 1x
    assert futures.safe_leverage(100, 101, False, 20) == 0  # stop on the wrong side


def test_a_short_twins_exit_works_before_the_twin_registered_it():
    df = pd.DataFrame({"close": [100 - i * 0.5 for i in range(60)]})
    df["open"], df["high"], df["low"] = df["close"], df["close"] + 0.1, df["close"] - 0.1
    engine.CUSTOM_EXITS.pop("flipped:rsi>70", None)  # as after a restart
    got = engine._one_trailing_exit("flipped:rsi>70", df)
    assert got is not None and bool(got.iloc[-1])  # falling price: overbought on the flipped chart


def test_futures_take_profit_is_pulled_in_to_the_cap(tmp_path):
    # Shawn's MAGMA/USDT: a 6R Donchian target, capped at 2R for Futures AI
    assert guards.load(str(tmp_path / "none.json"))["futures"]["max_rr"] == 2.0
    assert guards.load(str(tmp_path / "none.json"))["spot"]["max_rr"] == 0.0
    bars = [(100, 100.5, 99.9, 100.2), (100.2, 104.5, 100, 104)]  # reaches 104 = 2R, never 110
    far = _run(tmp_path / "far", bars)
    [pos] = far.state["open"].values()
    assert pos["signal"]["target"] == 110.0 and not far.state["trades"]
    near = _run(tmp_path / "near", bars, max_rr=2.0)
    [t] = near.state["trades"]
    assert t["exit_reason"] == "target" and t["pnl_usdt"] > 0
    # an open trade shows the capped target, and follows the setting when it changes
    held = _run(tmp_path / "held", bars[:1], max_rr=3.0)
    [pos] = held.state["open"].values()
    assert pos["signal"]["target"] == 106.0 and pos["full_target"] == 110.0


def test_capped_short_and_no_target():
    from aitrader.backtest.engine import capped

    s = Signal(ts=0, strategy="x", exchange="bybit", symbol="BTC/USDT", timeframe="1h", side=Side.SELL,
               entry=100.0, stop=102.0, target=80.0)
    assert capped(s, 2.0).target == 96.0 and capped(s, 0).target == 80.0 and capped(s, 20).target == 80.0
    assert capped(Signal(**{**s.__dict__, "target": None}), 2.0).target is None
