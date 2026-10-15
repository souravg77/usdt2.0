"""Timeframe picks: keep only the steady winners on one timeframe; the rest stop trading it."""

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.learn import Entry, validate_spec
from aitrader.learn import picks
from aitrader.learn.keep import keep_top
from aitrader.learn.registry import Registry, make_strategy, short_twin
from aitrader.learn.replay import replay
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_learn import EVERY_UP_BAR
from test_replay import END, START, make_store

UP = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))


def result(i, trades=20, total=5.0, up=3, down=1):
    return {"id": i, "name": i, "trades": trades, "win_rate": 50.0, "total_r": total,
            "quarters_up": up, "quarters_down": down, "quarters": {}}


def test_winners_need_a_positive_total_enough_trades_and_steady_quarters():
    rs = [result("good"), result("few", trades=5), result("losing", total=-1.0),
          result("one_lucky_quarter", up=1, down=3), result("even", up=2, down=2)]
    assert [r["id"] for r in picks.winners(rs)] == ["good", "even"]


def test_evaluate_counts_trades_by_quarter(tmp_path):
    store = make_store(tmp_path / "c.db")
    daily = Entry(id="d", kind="rule", name="d", spec=validate_spec(dict(EVERY_UP_BAR, name="d", timeframes=["1d"])))
    res = picks.evaluate([UP, daily], store, "bybit", ["BTC/USDT", "ETH/USDT"], "1h")
    assert [r["id"] for r in res] == ["up"]  # the daily one doesn't trade 1h
    r = res[0]
    assert r["trades"] > 0 and abs(sum(r["quarters"].values()) - r["total_r"]) < 0.05
    assert r["quarters_up"] + r["quarters_down"] == len(r["quarters"])
    store.close()


def test_a_pick_copies_the_winners_and_takes_the_timeframe_from_the_rest(tmp_path):
    reg = Registry(str(tmp_path / "registry.json"))
    other = Entry(id="other", kind="rule", name="other",
                  spec=validate_spec(dict(EVERY_UP_BAR, name="other", timeframes=["1h", "4h"])))
    for e in (UP, other):
        reg.add(e)
        e.set_status("paper", "test")
    short = short_twin(other)
    path = picks.picks_path(reg.path)
    out = picks.apply(reg, [UP, other, short], "4h", [result("other"), result(short.id), result("up", total=-2)],
                      path, info={"pairs": ["BTC/USDT"]})
    assert out["picked"] == ["other", short.id] and out["added"] == ["win4h-other", f"win4h-{short.id}"]
    copy, short_copy = reg.entries["win4h-other"], reg.entries[f"win4h-{short.id}"]
    assert copy.name == "WIN 4h: other" and short_copy.name == f"WIN 4h: {short.name}"
    assert copy.status == "paper" and make_strategy(copy).spec.timeframes == ("4h",)
    assert make_strategy(short_copy).spec.timeframes == ("4h",)
    tf_picks = picks.load(path)
    assert tf_picks["4h"]["picked"] == ["other", short.id] and tf_picks["4h"]["pairs"] == ["BTC/USDT"]
    # the original keeps its 1h trading but not 4h; the copy trades 4h only
    assert picks.allowed(other, ["1h", "4h"], tf_picks) == ["1h"]
    assert picks.allowed(copy, ["4h"], tf_picks) == ["4h"]
    assert picks.allowed(other, ["1h", "4h"], {}) == ["1h", "4h"]
    # keep-top leaves the pick copies alone
    keep_top(reg, 10)
    assert copy.status == "paper"
    # a new pick without "other" stops its copy; undo stops the rest
    again = picks.apply(Registry(reg.path), [UP, other, short], "4h", [result(short.id)], path)
    assert again["dropped"] == ["win4h-other"]
    reg = Registry(reg.path)
    assert picks.clear(reg, "4h", path) == [f"win4h-{short.id}"]
    assert picks.load(path) == {}


def test_replay_on_a_picked_timeframe_shows_only_the_kept_strategies(tmp_path):
    store = make_store(tmp_path / "c.db")
    tf_picks = {"1h": {"picked": []}}
    res = replay([UP], store, "bybit", "BTC/USDT", "1h", START, END, tf_picks=tf_picks)
    assert res["strategies"] == [] and res["trades"] == []
    kept = picks.pick_copy(UP, "1h")
    res = replay([UP, kept], store, "bybit", "BTC/USDT", "1h", START, END, tf_picks=tf_picks)
    assert [s["id"] for s in res["strategies"]] == ["win1h-up"] and res["trades"]
    assert res["strategies"][0]["name"] == "WIN 1h: up_bar"
    store.close()


def test_replay_page_starts_and_undoes_a_pick(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_store(tmp_path / "x.db").close()
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]), Auth("admin", "correct horse battery", "k"),
                     runner, Paths(report=str(tmp_path / "none.json"), registry=str(tmp_path / "registry.json")))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": "correct horse battery"})
    assert 'id="tf-pick"' in c.get("/replay").text
    csrf = Auth("admin", "correct horse battery", "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/replay/pick", data={"csrf": "x", "tf": "4h"}).status_code == 403
    r = c.post("/replay/pick", data={"csrf": csrf, "tf": "4h"}, follow_redirects=False)
    assert r.status_code == 303 and "tf=4h" in r.headers["location"]
    runner.wait_idle()
    job = [j for j in runner.list() if j["meta"].get("kind") == "pick"][0]
    assert job["steps"][0][:2] == ["download", "--since"] and job["steps"][0][-2:] == ["--timeframe", "4h"]
    assert job["steps"][1] == ["learn", "pick-timeframe", "4h", "--years", "2"]
    # a saved pick is listed with an undo button
    picks.save(picks.picks_path(str(tmp_path / "registry.json")),
               {"4h": {"at": 1, "picked": ["up"], "pairs": ["BTC/USDT"], "results": [result("up")]}})
    page = c.get("/replay?tf=4h").text
    assert "4h pick on: 1 strategies kept" in page and "Undo the 4h pick" in page
    c.post("/replay/pick", data={"csrf": csrf, "tf": "4h", "undo": "1"})
    runner.wait_idle()
    assert ["learn", "pick-timeframe", "4h", "--clear"] in [j["steps"][0] for j in runner.list()]


def test_cli_pick_timeframe_and_undo(tmp_path, monkeypatch, capsys):
    import time

    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    make_store(tmp_path / "data" / "aitrader.db").close()
    # the synthetic candles are from 2025: test enough years back to reach them
    years = str(round((time.time() * 1000 - START) / (365.25 * 86_400_000) + 1, 1))
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    args = ["--env-file", "none", "learn", "--symbol", "BTC/USDT", "pick-timeframe", "1h", "--years", years,
            "--min-trades", "1"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "1h: tested" in out
    tf_picks = picks.load("data/learn/timeframe_picks.json")
    assert "1h" in tf_picks and tf_picks["1h"]["results"]
    assert main(["--env-file", "none", "learn", "pick-timeframe", "1h", "--clear"]) == 0
    assert picks.load("data/learn/timeframe_picks.json") == {}


def test_a_kept_builtin_strategy_still_builds_under_its_win_name():
    from aitrader.strategies import STRATEGIES

    name = sorted(STRATEGIES)[0]
    orig = Entry(id=f"builtin-{name}", kind="builtin", name=name)
    c = picks.pick_copy(orig, "4h")
    assert c.name == f"WIN 4h: {name}" and make_strategy(c).spec.timeframes == ("4h",)
    # a copy saved before the name was kept in its spec still builds
    old = Entry(id=c.id, kind="builtin", name=c.name, spec={k: v for k, v in c.spec.items() if k != "builtin"})
    assert make_strategy(old).spec.timeframes == ("4h",)


def test_a_short_twin_of_a_win_copy_is_not_picked_again(tmp_path):
    store = make_store(tmp_path / "c.db")
    copy = picks.pick_copy(UP, "1h")
    twin_of_copy = short_twin(copy)
    assert picks.is_copy(twin_of_copy) and not picks.is_copy(short_twin(UP))
    res = picks.evaluate([UP, copy, twin_of_copy], store, "bybit", ["BTC/USDT"], "1h")
    assert [r["id"] for r in res] == ["up"]
    store.close()


def test_winners_only_switch(tmp_path):
    reg_path = str(tmp_path / "registry.json")
    assert not picks.winners_only(reg_path)
    copy = picks.pick_copy(UP, "4h")
    assert picks.trades_now(UP, False) and picks.trades_now(copy, False)
    picks.set_winners_only(reg_path, True, by="admin")
    assert picks.winners_only(reg_path)
    assert not picks.trades_now(UP, True) and picks.trades_now(copy, True)
    picks.set_winners_only(reg_path, False)
    assert not picks.winners_only(reg_path)


def test_trading_page_winners_only_button(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_store(tmp_path / "x.db").close()
    reg_path = str(tmp_path / "registry.json")
    reg = Registry(reg_path)
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    win = picks.pick_copy(UP, "4h")
    win.status = "paper"
    reg.add(win)
    reg.save()
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]), Auth("admin", "correct horse battery", "k"),
                     runner, Paths(report=str(tmp_path / "none.json"), registry=reg_path))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": "correct horse battery"})
    page = c.get("/trading").text
    assert "Trade only the WIN strategies" in page and '<span class="v good">2</span>' in page
    csrf = Auth("admin", "correct horse battery", "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/trading/paper/winners-only", data={"csrf": "x", "on": "1"}).status_code == 403
    c.post("/trading/paper/winners-only", data={"csrf": csrf, "on": "1"})
    assert picks.winners_only(reg_path)
    page = c.get("/trading").text
    assert "Trade the best strategies too" in page and '<span class="v good">1</span>' in page
    c.post("/trading/paper/winners-only", data={"csrf": csrf, "on": "0"})
    assert not picks.winners_only(reg_path)


def test_futures_mode_switches_the_whole_dashboard(tmp_path, monkeypatch):
    import asyncio
    import json

    from aitrader.learn import wallet
    from aitrader.web.live import LiveMarket

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AITRADER_LEVERAGE", raising=False)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    paths = Paths(report=str(tmp_path / "none.json"), registry=str(tmp_path / "registry.json"),
                  paper_state=str(tmp_path / "learn" / "paper_state.json"))
    (tmp_path / "learn").mkdir()
    key = "s1|bybit|BTC/USDT|1h"
    json.dump({"open": {key: {"entry_id": "s1", "filled": True, "entry": 100.0, "units": 1.0, "mark": 101.0,
                              "mark_r": 0.2, "perp": True, "leverage": 2.0,
                              "signal": {"ts": 1, "entry": 100.0, "stop": 95.0, "side": "buy"}}}},
              open(paths.paper_state, "w"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]),
                     Auth("admin", "correct horse battery", "k"), runner, paths)
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": "correct horse battery"})
    page = c.get("/trading").text
    assert 'action="/market-mode"' in page and "Spot AI workspace" in page and "Turn Futures AI off" in page
    # the Spot view hides the futures position; the Futures view shows it with its liquidation price
    assert "<th>Liquidation</th>" not in page and "long (perp)" not in page
    csrf = Auth("admin", "correct horse battery", "k").csrf(c.cookies.get(COOKIE))
    path = wallet.market_path(paths.paper_state)
    # the header switch, from any page, back to the same page
    assert c.post("/market-mode", data={"csrf": "x", "market": "futures"}).status_code == 403
    r = c.post("/market-mode", data={"csrf": csrf, "market": "futures", "back": "/pairs"}, follow_redirects=False)
    assert r.headers["location"].startswith("/pairs?msg=Showing") and wallet.futures_on(path)
    r = c.post("/market-mode", data={"csrf": csrf, "market": "futures", "back": "https://evil.example"},
               follow_redirects=False)
    assert r.headers["location"].startswith("/trading?")
    page = c.get("/trading").text
    assert "Futures AI 1x" in page and "Futures AI workspace" in page and "<th>Liquidation</th>" in page
    assert "long (perp)" in page and "Futures AI paper wallet" in page
    assert "50.5" in page  # a 2x long from 100: liquidation about 100 x (1 - 1/2 + 0.005)
    # Bybit's leverage choices for paper futures, 1x-100x
    assert '<option value="50">50x</option>' in page and '<option value="100">100x</option>' in page
    c.post("/trading/paper/leverage", data={"csrf": csrf, "lev": "500"})
    assert wallet.paper_leverage(path) == 100.0
    c.post("/trading/paper/leverage", data={"csrf": csrf, "lev": "25"})
    assert wallet.paper_leverage(path) == 25.0 and "Futures AI 25x" in c.get("/markets").text
    assert "perpetual" in c.get("/markets").text
    # each wallet keeps its own starting amount
    c.post("/trading/paper/wallet", data={"csrf": csrf, "amount": "500", "market": "futures"})
    assert wallet.load(wallet.wallet_path(paths.paper_state, "futures"))["start"] == 500.0
    assert wallet.load(wallet.wallet_path(paths.paper_state))["start"] == 10_000.0
    # the engines turn on and off one at a time
    eng = wallet.engines_path(paths.paper_state)
    c.post("/trading/paper/market", data={"csrf": csrf, "market": "futures", "on": "0"})
    assert wallet.engines(eng) == {"spot": True, "futures": False} and "Turn Futures AI on" in c.get("/trading").text
    c.post("/trading/paper/market", data={"csrf": csrf, "market": "futures", "on": "1"})
    assert wallet.engines(eng) == {"spot": True, "futures": True}
    # each engine's own limits
    c.post("/trading/paper/limits", data={"csrf": csrf, "market": "futures", "loss": "2.5", "max_open": "1"})
    assert wallet.limits(wallet.limits_path(paths.paper_state))["futures"] == {"daily_loss_pct": 2.5, "max_open": 1}
    assert wallet.limits(wallet.limits_path(paths.paper_state))["spot"]["max_open"] == wallet.DEFAULT_MAX_OPEN
    assert "PAUSED" in c.get("/trading").text and "Futures AI has 1 open positions" in c.get("/trading").text
    c.post("/market-mode", data={"csrf": csrf, "market": "spot"})
    assert not wallet.futures_on(path) and "Spot AI workspace" in c.get("/trading").text

    class Perps:
        asked = []

        async def fetch_tickers(self):
            return {"BTC/USDT:USDT": {"last": 80000.0, "percentage": 1.0, "info": {"fundingRate": "0.0001"}}}

        async def fetch_ohlcv(self, symbol, tf, limit=500):
            self.asked.append(symbol)
            return [[1, 1, 1, 1, 1, 1]]

    feed = LiveMarket(str(tmp_path / "x.db"), client=Perps(), perp=True)
    t = asyncio.run(feed.tickers(["BTC/USDT"]))
    assert t["tickers"][0]["funding"] == 0.0001 and t["tickers"][0]["price"] == 80000.0
    asyncio.run(feed.candles("BTC/USDT", "1h"))
    assert Perps.asked == ["BTC/USDT:USDT"]


def test_paper_leverage_is_cut_to_each_coins_bybit_maximum(tmp_path):
    import asyncio

    from aitrader.learn import wallet
    from aitrader.learn.paper import PaperTrader
    from aitrader.market import perp_limits
    from aitrader.trade import futures

    markets = {"BTC/USDT:USDT": {"swap": True, "linear": True, "quote": "USDT", "settle": "USDT", "base": "BTC",
                                 "limits": {"leverage": {"max": 100}}},
               "PEPE/USDT:USDT": {"swap": True, "linear": True, "quote": "USDT", "settle": "USDT", "base": "PEPE",
                                  "limits": {"leverage": {"max": 25}}},
               "PEPE/USDT": {"spot": True, "base": "PEPE", "quote": "USDT"}}

    class Client:
        async def load_markets(self):
            return markets

    lp = str(tmp_path / "limits.json")
    assert perp_limits.due(lp)
    asyncio.run(perp_limits.refresh(Client(), lp, now_ms=1000))
    assert perp_limits.max_leverage("PEPE/USDT", lp) == 25.0 and perp_limits.max_leverage("XRP/USDT", lp) is None
    assert not perp_limits.due(lp, now_ms=2000)
    assert futures.clamp_paper_leverage(50, 25) == 25 and futures.clamp_paper_leverage(500) == 100
    store = make_store(tmp_path / "c.db")
    state = str(tmp_path / "s.json")
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    trader.perp_limits_path = lp
    wallet.set_leverage(wallet.market_path(state), 50)
    assert trader._lev("BTC/USDT") == 50 and trader._lev("PEPE/USDT") == 25 and trader._lev("XRP/USDT") == 50
    store.close()
