"""Each paper engine's extra rules: loss breakers, minimum reward, higher-timeframe trend, split take-profit."""

import json
import os

import pandas as pd
import pytest

from aitrader.backtest import BacktestConfig
from aitrader.backtest.engine import run_backtest, simulate_signal, too_little_reward
from aitrader.learn import guards, wallet
from aitrader.models import Side
from aitrader.strategies.base import Signal

H = 3_600_000
FREE = BacktestConfig(taker_fee=0, maker_fee=0, slippage_bps=0, perp_taker_fee=0, perp_maker_fee=0,
                      perp_funding_8h=0, max_cost_r=0)


def _bars(rows):
    return pd.DataFrame([{"ts": i * H, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def _sig(side=Side.BUY, entry=100.0, stop=98.0, target=104.0):
    return Signal(ts=0, strategy="t", exchange="bybit", symbol="BTC/USDT", timeframe="1h", side=side,
                  entry=entry, stop=stop, target=target)


def test_split_take_profit_takes_half_halfway_and_the_rest_at_the_target():
    df = _bars([(100, 100, 100, 100), (100, 102.5, 99.5, 102), (102, 104.5, 101, 104)])
    one = simulate_signal(_sig(), df, 1, FREE, {})
    split = simulate_signal(_sig(), df, 1, BacktestConfig(**{**FREE.__dict__, "split_tp": True}), {})
    assert one.r == pytest.approx(2.0) and one.tp1 is None
    assert split.tp1 == 102.0 and split.r == pytest.approx(0.5 * 1 + 0.5 * 2)  # 1R on half, 2R on half
    # the first half is kept when the rest is stopped out
    df = _bars([(100, 100, 100, 100), (100, 102.5, 99.5, 102), (102, 102, 97, 97)])
    hurt = simulate_signal(_sig(), df, 1, BacktestConfig(**{**FREE.__dict__, "split_tp": True}), {})
    assert hurt.exit_reason == "stop" and hurt.r == pytest.approx(0.5 * 1 - 0.5 * 1)


def test_split_take_profit_on_a_short():
    df = _bars([(100, 100, 100, 100), (100, 100.5, 97.5, 98), (98, 99, 95.5, 96)])
    sig = _sig(Side.SELL, entry=100, stop=102, target=96)
    t = simulate_signal(sig, df, 1, BacktestConfig(**{**FREE.__dict__, "split_tp": True}), {})
    assert t.tp1 == 98.0 and t.exit_reason == "target" and t.r == pytest.approx(1.5)


def test_minimum_reward_skips_close_targets_but_not_rule_exits():
    cfg = BacktestConfig(min_rr=2.0)
    assert too_little_reward(_sig(target=103), cfg)  # 1.5R
    assert not too_little_reward(_sig(target=104), cfg)  # 2R
    assert not too_little_reward(_sig(target=None), cfg)  # exits on its own rule
    assert not too_little_reward(_sig(target=101), BacktestConfig())  # off

    class One:
        def __init__(self, target):
            self.target = target

        def generate(self, df, ex, sym, tf):
            return [_sig(target=self.target)]

    df = _bars([(100, 100, 100, 100)] * 6)
    run = lambda target, rr: run_backtest(One(target), df, "bybit", "BTC/USDT", "1h",  # noqa: E731
                                          BacktestConfig(max_cost_r=0, min_rr=rr))
    assert len(run(103, 0)) == 1 and run(103, 2) == [] and len(run(104, 2)) == 1


def test_settings_have_safe_defaults_and_limits(tmp_path):
    path = guards.path_for(str(tmp_path / "paper_state.json"))
    g = guards.load(path)
    assert g["futures"] == guards.DEFAULTS and g["spot"]["breaker"] is True
    got = guards.save(path, "futures", {"min_rr": "2", "trend": True, "split_tp": True, "day_pause_pct": "99",
                                        "breaker": False})
    assert got["min_rr"] == 2 and got["trend"] and got["split_tp"] and got["day_pause_pct"] == 50.0
    assert got["breaker"] is False and guards.load(path)["spot"] == guards.DEFAULTS  # the other engine kept
    with pytest.raises(ValueError):
        guards.save(path, "futures", {"min_rr": "two"})
    with pytest.raises(ValueError):
        guards.save(path, "both", {})


def _state(*trades, open_=None):
    return {"trades": [{"exit_ts": ts, "pnl_usdt": p, "side": "sell", "perp": True} for ts, p in trades],
            "open": open_ or {}}


def test_a_three_percent_day_loss_pauses_the_engine_for_a_day():
    now = 1_760_500_000_000  # a Wednesday afternoon in India
    g = guards.DEFAULTS
    w = {"start": 1000.0, "since": 0}
    st = _state((now - 3_600_000, -20.0))
    assert guards.update_breaker(st, w, "futures", g, now) is None  # 2% today
    # an open trade down 10 USDT makes it 3%
    st["open"] = {"s|bybit|X|1h": {"filled": True, "perp": True, "units": 5.0, "mark_r": -1.0,
                                   "signal": {"entry": 10.0, "stop": 12.0, "side": "sell"}}}
    br = guards.update_breaker(st, w, "futures", g, now)
    assert br["kind"] == "day" and br["until"] == now + 24 * H and not br["close"]
    assert "3% pause" in guards.block(st, "futures", now + H) and guards.block(st, "spot", now) == ""
    assert guards.block(st, "futures", now + 25 * H) == ""
    assert guards.update_breaker(st, w, "futures", {**g, "breaker": False}, now + H) is None  # turned off


def test_a_six_percent_week_loss_closes_everything_until_monday():
    now = 1_760_500_000_000
    week0 = guards.week_start_ms(now)
    st = _state((week0 + 1000, -40.0), (now - 1000, -25.0))  # 6.5% this week (2.5% today)
    br = guards.update_breaker(st, {"start": 1000.0, "since": 0}, "futures", guards.DEFAULTS, now)
    assert br["kind"] == "week" and br["close"] and br["until"] == week0 + 7 * 86_400_000
    assert "until Monday" in br["why"]


def test_the_paper_trader_closes_an_engines_positions_at_its_weekly_limit(tmp_path):
    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    i = 600
    while not any(p["filled"] for p in trader.state["open"].values()):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        i += 1
    now = int(df.ts.iloc[i]) + H
    trader.state["breakers"] = {"spot": {"kind": "week", "at": now, "until": now + 10 * H, "close": True,
                                         "why": "Spot AI lost too much this week"}}
    trader.tick(now_ms=now)
    assert not trader.state["open"] and trader.state["trades"][-1]["exit_reason"] == "weekly loss limit"
    assert trader.state["breakers"]["spot"]["close"] is False  # done; still no new trades
    trader.tick(now_ms=int(df.ts.iloc[i + 1]) + H)
    assert not trader.state["open"]
    assert any(s["skipped"] == "loss breaker" for s in trader.state["signals"])
    store.close()


def test_trend_check():
    up = pd.DataFrame({"close": [100 + i for i in range(80)]})
    down = pd.DataFrame({"close": [200 - i for i in range(80)]})
    assert guards.trend_of(up) == "up" and guards.trend_of(down) == "down"
    assert guards.trend_of(up.head(30)) == "flat" and guards.trend_of(None) == "flat"
    assert guards.against(False, {"4h": "up", "1d": "down"}) == "1d"  # a long against the daily trend
    assert guards.against(True, {"4h": "down", "1d": "flat"}) == ""  # a short with the trend
    assert guards.HIGHER["15m"] == ("4h", "1d") and "1d" not in guards.HIGHER


def test_trading_page_sets_the_rules(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_web import PW, login

    from aitrader.config import Settings
    from aitrader.web import Auth, JobRunner, Paths, create_app

    monkeypatch.chdir(tmp_path)
    paths = Paths()
    os.makedirs("data/learn")
    json.dump({"open": {}, "trades": []}, open(paths.paper_state, "w"))
    client = TestClient(create_app(Settings(), Auth("admin", PW, "k"), JobRunner(str(tmp_path / "jobs"), env_file="none"),
                                   paths))
    login(client)
    page = client.get("/trading").text
    assert "Loss breakers" in page and "Min reward R" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/trading/paper/guards", data={"csrf": csrf, "market": "futures", "breaker": "1",
                                                   "day_pause_pct": "3", "pause_hours": "24", "week_close_pct": "6",
                                                   "min_rr": "2", "trend": "1", "split_tp": "1"},
                    follow_redirects=False)
    assert "bad=1" not in r.headers["location"]
    g = guards.load(guards.path_for(paths.paper_state))
    assert g["futures"]["min_rr"] == 2 and g["futures"]["trend"] and g["spot"] == guards.DEFAULTS
    r = client.post("/trading/paper/guards", data={"csrf": csrf, "market": "futures", "min_rr": "x"},
                    follow_redirects=False)
    assert "bad=1" in r.headers["location"]


def test_the_paper_trader_skips_a_long_against_the_daily_trend(tmp_path, monkeypatch):
    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    guards.save(guards.path_for(state), "spot", {"trend": True})
    looked = []
    monkeypatch.setattr(guards, "trend_of", lambda d: looked.append(d) or "down")
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    assert not trader.state["open"] and not trader.state["trades"] and looked
    why = [s for s in trader.state["signals"] if s["skipped"] == "against the higher-timeframe trend"]
    assert why and "4h trend points down" in why[0]["detail"]
    store.close()


def test_a_limit_entry_pays_the_maker_fee_or_misses_the_trade():
    from aitrader.backtest.engine import maker_fill

    cfg = BacktestConfig(taker_fee=0.001, maker_fee=0.0002, slippage_bps=5, max_cost_r=0)
    df = _bars([(100, 100, 100, 100), (100, 104.5, 99.5, 104)])  # dips below 100 first, then the target
    taker = simulate_signal(_sig(), df, 1, cfg, {})
    maker = simulate_signal(_sig(), df, 1, BacktestConfig(**{**cfg.__dict__, "maker_entry": True}), {})
    assert maker_fill(_sig(), df, 1) == 100.0 and maker.entry == 100.0 and taker.entry > 100.0
    assert maker.fee_r < taker.fee_r and maker.r > taker.r and maker.slippage_r == 0
    ran = _bars([(100, 100, 100, 100), (100.2, 104.5, 100.2, 104)])  # never trades below the open
    assert maker_fill(_sig(), ran, 1) is None
    assert simulate_signal(_sig(), ran, 1, BacktestConfig(**{**cfg.__dict__, "maker_entry": True}), {}) is None
    short = _sig(Side.SELL, entry=100, stop=102, target=96)
    assert maker_fill(short, _bars([(0, 0, 0, 0), (100, 100.5, 96, 97)]), 1) == 100.0
    assert maker_fill(short, _bars([(0, 0, 0, 0), (99.8, 99.8, 96, 97)]), 1) is None


def test_the_paper_trader_says_when_a_limit_entry_was_not_filled(tmp_path, monkeypatch):
    from aitrader.backtest import engine

    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    monkeypatch.setattr(engine, "maker_fill", lambda sig, df, pos: None)  # price always runs away
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    guards.save(guards.path_for(state), "spot", {"maker_entry": True})
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    store.close()
    assert not trader.state["trades"]
    assert any(s["skipped"] == "limit order not filled" for s in trader.state["signals"])
