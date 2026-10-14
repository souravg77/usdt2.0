"""The paper wallet: one starting amount (100-10,000 USDT) every paper strategy trades from."""

import json
import os

import pytest

from aitrader.learn import wallet


def test_wallet_balance_counts_trades_since_it_was_set(tmp_path):
    path = wallet.wallet_path(str(tmp_path / "learn" / "paper_state.json"))
    assert path == str(tmp_path / "learn" / "paper_wallet.json")
    assert wallet.load(path) == {"start": 10_000.0, "since": 0}  # no file: the default, every trade
    state = {"trades": [{"exit_ts": 100, "pnl_usdt": 50.0}, {"exit_ts": 300, "pnl_usdt": -20.0}],
             "open": {"a": {"filled": True, "units": 2.0, "entry": 30.0},
                      "b": {"filled": True, "units": 1.0, "entry": 90.0, "leverage": 3.0},  # perp margin
                      "c": {"filled": False, "units": 9.0, "entry": 9.0}}}
    assert wallet.balance(state, wallet.load(path)) == 10_030.0
    w = wallet.save(path, 500, now_ms=200)
    assert w == {"start": 500.0, "since": 200} and wallet.load(path) == w
    sm = wallet.summary(state, w)
    assert sm["balance"] == 480.0 and sm["in_use"] == 90.0 and sm["free"] == 390.0
    assert sm["trades"] == 1 and sm["closed_pnl"] == -20.0 and sm["open"] == 2
    assert wallet.save(path, 5)["start"] == 100.0 and wallet.save(path, 1e6)["start"] == 10_000.0  # 100-10,000
    with open(path, "w") as fh:
        fh.write("not json")
    assert wallet.load(path)["start"] == 10_000.0


def test_paper_trades_are_sized_from_the_wallet(tmp_path):
    from test_learn import EVERY_UP_BAR, H, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    def run(start):
        d = tmp_path / str(start)
        os.makedirs(d)
        store = CandleStore(str(d / "c.db"))
        df = synthetic(n=700)
        fill_store(store, df)
        reg = Registry(str(d / "reg.json"))
        reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
        reg.save()
        state = str(d / "paper_state.json")
        if start:
            wallet.save(wallet.wallet_path(state), start, now_ms=0)
        trader = PaperTrader(store, str(d / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
        for i in range(600, 700):
            trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        store.close()
        return trader.state

    big, small = run(10_000), run(100)
    first = lambda st: next(t for t in st["trades"])  # noqa: E731
    assert first(big)["units"] == pytest.approx(100 * first(small)["units"], rel=1e-6)  # 100x the money
    assert all(t["units"] * t["entry"] <= 50 + 1e-6 for t in small["trades"])  # at most half of 100 USDT
    assert "wallet_after" in first(small)
    assert wallet.balance(small, wallet.load(str(tmp_path / "100" / "paper_wallet.json"))) == \
        pytest.approx(100 + sum(t["pnl_usdt"] for t in small["trades"]))


def test_a_trade_under_bybits_minimum_order_is_skipped(tmp_path, monkeypatch):
    from test_learn import EVERY_UP_BAR, H, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn.rules import validate_spec

    monkeypatch.setattr(wallet, "MIN_ORDER_USDT", 1e9)  # every order is too small
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"],
                         state_path=str(tmp_path / "paper_state.json"))
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    assert trader.state["trades"] == [] and not any(p["filled"] for p in trader.state["open"].values())
    skipped = [s for s in trader.state["signals"] if s["skipped"]]
    assert skipped and "minimum order" in skipped[0]["skipped"] and "USDT wallet" in skipped[0]["detail"]
    store.close()


def test_trading_page_sets_the_wallet(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_web import PW, login

    from aitrader.config import Settings
    from aitrader.web import Auth, JobRunner, Paths, create_app

    monkeypatch.chdir(tmp_path)
    paths = Paths()
    os.makedirs("data/learn")
    json.dump({"open": {}, "trades": [{"exit_ts": 5, "pnl_usdt": 12.5}]}, open(paths.paper_state, "w"))
    client = TestClient(create_app(Settings(), Auth("admin", PW, "k"), JobRunner(str(tmp_path / "jobs"), env_file="none"),
                                   paths))
    token = login(client)
    page = client.get("/trading").text
    assert "Paper wallet (fake money)" in page and "10,012.50 USDT" in page  # default 10,000 + 12.50
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/trading/paper/wallet", data={"csrf": csrf, "amount": "50"}, follow_redirects=False)
    assert "bad=1" in r.headers["location"] and not os.path.exists("data/learn/paper_wallet.json")
    r = client.post("/trading/paper/wallet", data={"csrf": csrf, "amount": "1,000"}, follow_redirects=False)
    assert "bad=1" not in r.headers["location"]
    assert wallet.load("data/learn/paper_wallet.json")["start"] == 1000.0
    page = client.get("/trading").text
    assert "Balance <b>1,000.00 USDT</b>" in page and 'value="1000"' in page
    assert "Wallet <b>1,000.00 USDT</b>" in client.get("/").text
    assert token
