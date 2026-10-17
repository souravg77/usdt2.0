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
    assert "Amount per trade USDT" in client.get("/trading").text
    page = client.get("/trading").text
    assert 'action="/trading/paper/timeframes"' in page and 'name="tf" value="1h" checked' in page
    r = client.post("/trading/paper/timeframes", data={"csrf": csrf, "market": "futures", "tf": ["1h", "4h", "1d"]},
                    follow_redirects=False)
    assert "bad=1" not in r.headers["location"] and "1h%2C%204h%2C%201d" in r.headers["location"]
    g = guards.load(guards.path_for(paths.paper_state))
    assert g["futures"]["timeframes"] == ["1h", "4h", "1d"] and g["futures"]["min_rr"] == 2  # other rules kept
    assert 'name="tf" value="5m" checked' in client.get("/trading").text  # the Spot view: Spot AI's, unchanged
    r = client.post("/trading/paper/guards", data={"csrf": csrf, "market": "spot", "trade_usdt": "25"},
                    follow_redirects=False)
    assert "bad=1" not in r.headers["location"]
    g = guards.load(guards.path_for(paths.paper_state))
    assert g["spot"]["trade_usdt"] == 25 and g["futures"]["trade_usdt"] == 0
    assert "Strategies that must agree" in client.get("/trading").text
    r = client.post("/trading/paper/guards", data={"csrf": csrf, "market": "futures", "agree": "3"},
                    follow_redirects=False)
    assert "bad=1" not in r.headers["location"] and "3%20strategies%20agree" in r.headers["location"]
    g = guards.load(guards.path_for(paths.paper_state))
    assert g["futures"]["agree"] == 3 and g["spot"]["agree"] == 1


def test_the_amount_per_trade_is_0_for_automatic_or_at_least_5(tmp_path):
    path = str(tmp_path / "g.json")
    assert guards.load(path)["spot"]["trade_usdt"] == 0
    assert guards.save(path, "spot", {"trade_usdt": "0"})["trade_usdt"] == 0
    assert guards.save(path, "spot", {"trade_usdt": "2"})["trade_usdt"] == 5
    assert guards.save(path, "spot", {"trade_usdt": "-3"})["trade_usdt"] == 5
    assert guards.save(path, "futures", {"trade_usdt": "120.5"})["trade_usdt"] == 120.5


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


def test_correlation_of_hourly_returns():
    idx = list(range(100))
    a = pd.Series([((i * 7) % 11) / 100 for i in idx], index=idx)
    assert guards.correlation(a, a * 2) == pytest.approx(1.0)
    assert guards.correlation(a, -a) == pytest.approx(-1.0)
    assert guards.correlation(a, a.head(10)) == 0.0  # too few hours in common
    assert guards.correlation(a, pd.Series(0.0, index=idx)) == 0.0  # a flat series


def _two_pair_trader(tmp_path, values):
    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df, sym="BTC/USDT")
    fill_store(store, df, sym="ETH/USDT")  # the same prices: they move together exactly
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    guards.save(guards.path_for(state), "spot", values)
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT", "ETH/USDT"], state_path=state)
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    store.close()
    return trader


def test_an_engine_holds_at_most_its_cap_of_longs(tmp_path):
    trader = _two_pair_trader(tmp_path, {"max_long": 1, "max_correlated": 50})
    held = {k.split("|")[2] for k in trader.state["open"]} | {t["key"].split("|")[2] for t in trader.state["trades"]}
    why = [s for s in trader.state["signals"] if s["skipped"] == "exposure cap"]
    assert why and "already holds 1 long positions (its limit is 1)" in why[0]["detail"]
    assert len(trader.state["open"]) <= 1 and held


def test_pairs_that_move_together_count_against_one_cap(tmp_path):
    trader = _two_pair_trader(tmp_path, {"max_correlated": 1})
    why = [s for s in trader.state["signals"] if s["skipped"] == "exposure cap"]
    assert why and "move with" in why[0]["detail"]
    assert len({k.split("|")[2] for k in trader.state["open"]}) <= 1  # never both pairs at once


def test_a_limit_entry_pays_the_maker_fee_or_misses_the_trade():
    from aitrader.backtest.engine import maker_fill

    cfg = BacktestConfig(taker_fee=0.001, maker_fee=0.0002, slippage_bps=5, max_cost_r=0)
    df = _bars([(100, 100, 100, 100), (100, 101, 99.5, 100.5), (100.5, 104.5, 100.5, 104)])  # fills, then the target
    same = _bars([(100, 100, 100, 100), (100, 104.5, 99.5, 104)])  # target and fill in one candle
    assert simulate_signal(_sig(), same, 1, BacktestConfig(**{**cfg.__dict__, "maker_entry": True}), {}).exit_reason \
        != "target"  # the target may have come before the fill: not counted
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


def test_week_limit_still_checked_during_a_day_pause_and_reset_wallet_starts_fresh():
    now = guards.week_start_ms(1_760_000_000_000) + 3 * 86_400_000
    g = {**guards.DEFAULTS}
    w = {"start": 1000.0, "since": 0}
    state = {"trades": [], "open": {"s|bybit|BTC/USDT|1h|futures": {"perp": True, "units": 1, "entry": 100,
                                                                     "signal": {"side": "sell"}}},
             "breakers": {"futures": {"kind": "day", "until": now + 3_600_000, "close": False, "why": "day"}}}
    import aitrader.learn.guards as gm

    orig = gm.open_pnl
    gm.open_pnl = lambda st, m: -900.0  # the open short keeps losing during the pause
    try:
        br = guards.update_breaker(state, w, "futures", g, now)
        assert br["kind"] == "week" and br["close"]
        # a wallet reset an hour ago doesn't carry this week's earlier losses
        gm.open_pnl = lambda st, m: 0.0
        state = {"trades": [{"exit_ts": now - 86_400_000, "pnl_usdt": -500.0, "perp": True,
                             "key": "s|bybit|BTC/USDT|1h|futures"}], "open": {}}
        assert guards.update_breaker(state, {"start": 1000.0, "since": now - 3_600_000}, "futures", g, now) is None
        assert guards.update_breaker(state, {"start": 1000.0, "since": 0}, "futures", g, now)["kind"] == "week"
    finally:
        gm.open_pnl = orig


def test_nan_and_inf_are_not_saved_as_limits(tmp_path):
    path = str(tmp_path / "g.json")
    for bad in ("nan", "inf"):
        with pytest.raises(ValueError):
            guards.save(path, "spot", {"min_rr": bad})
    assert guards._clean({"min_rr": float("nan"), "day_pause_pct": float("inf")})["day_pause_pct"] == \
        guards.DEFAULTS["day_pause_pct"]


def test_the_amount_per_trade_has_its_own_save_button(tmp_path, monkeypatch):
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
    assert 'action="/trading/paper/amount"' in page and "Save amount" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/trading/paper/amount", data={"csrf": csrf, "market": "futures", "trade_usdt": "10"},
                    follow_redirects=False)
    assert "bad=1" not in r.headers["location"]
    path = guards.path_for(paths.paper_state)
    assert guards.load(path)["futures"]["trade_usdt"] == 10
    # the other rules' Set rules button keeps the saved amount
    client.post("/trading/paper/guards", data={"csrf": csrf, "market": "futures", "breaker": "1", "min_rr": "2"},
                follow_redirects=False)
    assert guards.load(path)["futures"]["trade_usdt"] == 10 and guards.load(path)["futures"]["min_rr"] == 2
    assert "10.00 USDT per trade" in client.get("/trading").text
    from aitrader.learn import wallet

    wallet.set_market(wallet.market_path(paths.paper_state), True)
    wallet.set_leverage(wallet.market_path(paths.paper_state), 20)
    assert "now 10.00 USDT margin × 20x = 200.00 USDT position per trade" in client.get("/trading").text
    wallet.set_market(wallet.market_path(paths.paper_state), False)
    r = client.post("/trading/paper/amount", data={"csrf": csrf, "market": "futures", "trade_usdt": "ten"},
                    follow_redirects=False)
    assert "bad=1" in r.headers["location"]


def test_breakeven_after_the_first_half_means_the_rest_cannot_lose():
    """From Gemini's file (Shawn chose to add it): stop to breakeven once the first half is taken."""
    split = BacktestConfig(**{**FREE.__dict__, "split_tp": True})
    be = BacktestConfig(**{**FREE.__dict__, "split_tp": True, "breakeven": True})
    df = _bars([(100, 100, 100, 100), (100, 102.5, 99.5, 102), (102, 102, 97, 97)])
    assert simulate_signal(_sig(), df, 1, split, {}).r == pytest.approx(0.0)  # 1R on half, -1R on half
    t = simulate_signal(_sig(), df, 1, be, {})
    assert t.exit_reason == "breakeven" and t.r == pytest.approx(0.5)  # 1R on half, 0 on the rest
    # the stop moves only from the candle after the first half: that candle's own low still uses the old stop
    df = _bars([(100, 100, 100, 100), (100, 102.5, 99.0, 102), (102, 104.5, 101, 104)])
    assert simulate_signal(_sig(), df, 1, be, {}).r == pytest.approx(1.5)
    short = _sig(Side.SELL, entry=100, stop=102, target=96)
    df = _bars([(100, 100, 100, 100), (100, 100.5, 97.5, 98), (98, 103, 98, 103)])
    t = simulate_signal(short, df, 1, be, {})
    assert t.exit_reason == "breakeven" and t.r == pytest.approx(0.5)
    # with costs the breakeven stop pays them back: the rest ends at about 0R, not a loss
    costs = BacktestConfig(split_tp=True, breakeven=True, max_cost_r=0)
    df = _bars([(100, 100, 100, 100), (100, 102.5, 99.5, 102), (102, 102, 97, 97)])
    t = simulate_signal(_sig(), df, 1, costs, {})
    half = simulate_signal(_sig(), df, 1, BacktestConfig(split_tp=True, max_cost_r=0), {})
    assert t.r > half.r and t.r > 0.4
    assert guards.DEFAULTS["breakeven"] is False  # off until a person turns it on


def test_each_engine_trades_only_the_timeframes_chosen(tmp_path):
    """Shawn: Futures AI takes 1h, 4h and 1d trades only."""
    path = guards.path_for(str(tmp_path / "paper_state.json"))
    assert guards.load(path)["futures"]["timeframes"] == list(guards.TIMEFRAMES)  # every one by default
    got = guards.save(path, "futures", {"timeframes": ["1d", "1h", "4h", "2w"]})
    assert got["timeframes"] == ["1h", "4h", "1d"]  # unknown ones dropped, in timeframe order
    assert guards.load(path)["spot"]["timeframes"] == list(guards.TIMEFRAMES)  # the other engine unchanged
    got = guards.save(path, "futures", {"min_rr": "2"})  # saving other rules keeps the timeframes
    assert got["timeframes"] == ["1h", "4h", "1d"] and got["min_rr"] == 2


def _agree_trader(tmp_path, n_strategies, agree):
    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    for i in range(n_strategies):
        reg.add(Entry(id=f"up{i}", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    guards.save(guards.path_for(state), "spot", {"agree": agree})
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    store.close()
    return trader


def test_a_trade_opens_only_when_enough_strategies_agree(tmp_path):
    """Shawn: a trade opens once 3 strategies agree on it."""
    alone = _agree_trader(tmp_path / "a", 2, 1)
    opened = [s for s in alone.state["signals"] if s["skipped"] == ""]
    assert len({s["key"].split("|")[0] for s in opened}) == 2  # rule off: each strategy trades alone
    two = _agree_trader(tmp_path / "b", 2, 3)
    assert not [s for s in two.state["signals"] if s["skipped"] == ""]
    no = [s for s in two.state["signals"] if s["skipped"] == "strategies don't agree"]
    assert no and "2 of the 3 strategies needed" in no[0]["detail"]
    three = _agree_trader(tmp_path / "c", 3, 3)
    opened = [s for s in three.state["signals"] if s["skipped"] == ""]
    assert opened and all(s["detail"] == "3 strategies agreed" for s in opened)
    # one trade per agreement, not three: the others say who took it
    by_ts = {}
    for s in three.state["signals"]:
        by_ts.setdefault(s["ts"], []).append(s["skipped"])
    for ts in {s["ts"] for s in opened}:
        assert sorted(by_ts[ts]) == ["", "another agreeing strategy took it", "another agreeing strategy took it"]
    agreed = [p for p in three.state["open"].values() if p.get("agreed")] + \
        [t for t in three.state["trades"] if t.get("agreed")]
    assert all(set(p["agreed"]) == {"up0", "up1", "up2"} for p in agreed)
