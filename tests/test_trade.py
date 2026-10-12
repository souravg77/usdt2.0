"""Phases 5 and 6: risk engine, pause/kill, event log, executor, go-live gate, Telegram, reports."""

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from aitrader.learn import Entry, Registry
from aitrader.models import Balance, Order, OrderBook, OrderStatus, OrderType, Side
from aitrader.ops import EventLog, Heartbeat
from aitrader.ops.telegram import Telegram, TelegramService
from aitrader.trade import (HARD_MAX_RISK, Control, Executor, GateConfig, GoLive, RiskEngine,
                            RiskLimits, client_id)
from aitrader.trade.report import daily_text, status_text, weekly, weekly_text

HOUR = 3_600_000
NOW = 1_760_000_000.0  # fixed clock (seconds)


# ---- risk -------------------------------------------------------------------------------------


def test_risk_sizing_and_caps():
    eng = RiskEngine(RiskLimits(risk_per_trade=0.005, max_coin_exposure_pct=0.25))
    s = eng.size("BTC/USDT", 10_000, 10_000, entry=100, stop=99, signal_risk=0.01, size_mult=1.0,
                 open_positions=[])
    # risk 0.5% of 10k = 50 USDT over a 1 USDT stop = 50 units = 5000 USDT -> cut to 25% coin cap
    assert s.ok and s.notional == pytest.approx(2500) and "coin exposure" in s.reasons[0]
    s = eng.size("BTC/USDT", 10_000, 10_000, 100, 90, 0.01, 0.5, [])
    assert s.risk_usdt == pytest.approx(25)  # half size from the filters
    assert not eng.size("BTC/USDT", 10_000, 10_000, 100, 101, 0.01, 1, []).ok  # stop above entry
    full = [{"symbol": f"C{i}/USDT", "notional": 100} for i in range(3)]
    assert "positions open" in eng.size("BTC/USDT", 10_000, 10_000, 100, 90, 0.01, 1, full).reasons[0]
    tiny = eng.size("BTC/USDT", 100, 100, 100, 50, 0.005, 1, [])
    assert not tiny.ok and "minimum" in tiny.reasons[-1]


def test_risk_defaults_follow_the_agreed_guardrails():
    lim = RiskLimits()
    assert lim.risk_per_trade == 0.0025 and lim.daily_loss_pct == 0.01


def test_correlated_coins_count_as_one_group(monkeypatch):
    eng = RiskEngine(RiskLimits(max_open_positions=5))
    two = [{"symbol": "BTC/USDT", "notional": 100}, {"symbol": "ETH/USDT", "notional": 100}]
    s = eng.size("SOL/USDT", 10_000, 10_000, 100, 90, 0.01, 1, two)
    assert not s.ok and "correlated group BTC/ETH/SOL" in s.reasons[0]
    assert eng.size("DOGE/USDT", 10_000, 10_000, 100, 90, 0.01, 1, two).ok  # not in the group
    big = [{"symbol": "BTC/USDT", "notional": 3_900}]
    s = eng.size("ETH/USDT", 10_000, 10_000, 100, 99.9, 0.01, 1, big)
    assert s.ok and s.notional == pytest.approx(100) and "correlated group exposure" in s.reasons[-1]
    monkeypatch.setenv("AITRADER_CORRELATION_GROUPS", "doge, shib; BTC,ETH")
    assert RiskLimits.from_env().correlation_groups == (("DOGE", "SHIB"), ("BTC", "ETH"))


def test_risk_limits_env_never_above_hard_cap(monkeypatch):
    monkeypatch.setenv("AITRADER_RISK_PER_TRADE", "0.05")
    assert RiskLimits.from_env().risk_per_trade == HARD_MAX_RISK


def test_loss_breach_daily_and_weekly():
    eng = RiskEngine(RiskLimits(daily_loss_pct=0.02, weekly_loss_pct=0.05))
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)  # a Thursday
    today = int(now.timestamp() * 1000) - HOUR
    assert eng.loss_breach(9_850, [{"exit_ts": today, "pnl_usdt": -150}], now) is None
    assert "daily" in eng.loss_breach(9_750, [{"exit_ts": today, "pnl_usdt": -250}], now)
    monday = int(datetime(2026, 10, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    closed = [{"exit_ts": monday, "pnl_usdt": -190}] * 3
    assert "weekly" in eng.loss_breach(9_430, closed, now)


# ---- control and events -----------------------------------------------------------------------


def test_control_pause_kill_resume(tmp_path):
    c = Control(str(tmp_path / "control.json"))
    assert not c.paused and not c.killed
    c.pause("daily loss", by="risk")
    c.pause("second reason")
    assert c.paused and c.state()["reason"] == "daily loss"
    c.kill(by="me")
    assert c.killed and c.paused
    c.resume(by="me")
    assert not c.paused and not c.killed


def test_event_log_tail_and_heartbeat(tmp_path):
    log = EventLog(str(tmp_path / "ev.jsonl"), service="paper")
    log.emit("trade_open", "a", key="k")
    evs, off = log.tail(0)
    assert [e["kind"] for e in evs] == ["trade_open"] and evs[0]["data"]["key"] == "k"
    with open(log.path, "a") as fh:
        fh.write('{"half": ')  # a line still being written is not returned
    hb = Heartbeat(log, every_s=300)
    evs2, off2 = log.tail(off)
    assert evs2 == [] and off2 == off
    with open(log.path, "a") as fh:
        fh.write('1}\n')
    hb.beat()
    hb.beat()  # too soon: ignored
    assert [e["kind"] for e in log.read(kinds={"heartbeat"})] == ["heartbeat"]


# ---- the executor -----------------------------------------------------------------------------


class FakeAdapter:
    """Fills market orders at the book price; can be told to reject, half-fill or time out."""

    name = "bybit"

    def __init__(self, usdt=10_000.0, bid=100.0, ask=100.1):
        self.cash = {"USDT": usdt}
        self.bid, self.ask = bid, ask
        self.orders: dict[str, Order] = {}
        self.sent: list[Order] = []
        self.mode = "fill"  # fill | reject | partial | timeout | lose
        self.fail = None

    async def get_orderbook(self, symbol, depth=20):
        if self.fail:
            raise self.fail
        return OrderBook("bybit", symbol, 0, [(self.bid, 10)], [(self.ask, 10)])

    async def balance(self):
        if self.fail:
            raise self.fail
        return Balance("bybit", free=dict(self.cash))

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason="",
                          client_id=None):
        if client_id in self.orders:
            return self.orders[client_id]
        base = symbol.split("/")[0]
        px = self.ask if side is Side.BUY else self.bid
        if self.mode == "reject":
            o = Order(client_id, "bybit", symbol, side, type, amount, None, OrderStatus.REJECTED,
                      reason="insufficient balance")
        else:
            filled = amount / 2 if self.mode == "partial" else amount
            o = Order(client_id, "bybit", symbol, side, type, amount, None,
                      OrderStatus.FILLED if filled == amount else OrderStatus.CANCELED,
                      filled=filled, average=px, fee=filled * px * 0.001, fee_currency="USDT")
            sign = 1 if side is Side.BUY else -1
            self.cash["USDT"] -= sign * filled * px + o.fee
            self.cash[base] = self.cash.get(base, 0.0) + sign * filled
        self.orders[client_id] = o
        self.sent.append(o)
        if self.mode == "timeout":
            raise TimeoutError("request timed out")  # the order still reached the exchange
        if self.mode == "lose":
            del self.orders[client_id]
            self.cash = {"USDT": self.cash["USDT"]}
            raise TimeoutError("lost")
        return o

    async def find_order(self, cid, symbol):
        return self.orders.get(cid)


def _paper(path, open_=None, trades=None):
    with open(path, "w") as fh:
        json.dump({"open": open_ or {}, "trades": trades or []}, fh)
    os.utime(path, (NOW, NOW))


def _pos(ts, stop=95.0, ai=0.7):
    return {"entry_id": "s1", "signal": {"ts": ts, "entry": 100.0, "stop": stop, "target": 110.0,
                                         "risk_pct": 0.005}, "size_mult": 1.0, "ai_prob": ai}


KEY = "s1|bybit|BTC/USDT|1h"
SIG = int(NOW * 1000) - HOUR


@pytest.fixture
def ex(tmp_path):
    paper = tmp_path / "paper.json"
    _paper(paper, {KEY: _pos(SIG)})
    adapter = FakeAdapter()
    events = EventLog(str(tmp_path / "ev.jsonl"), service="executor-live")

    def make(**kw):
        kw.setdefault("live_guard", lambda sid: [])
        return Executor(adapter, kw.pop("mode", "live"), "bybit", str(paper),
                        state_path=str(tmp_path / "state.json"), control=Control(str(tmp_path / "c.json")),
                        events=events, clock=lambda: NOW, **kw)

    return make, adapter, paper, events


def run(coro):
    return asyncio.run(coro)


def test_executor_mirrors_paper_entry_and_exit(ex):
    make, adapter, paper, events = ex
    e = make()
    out = run(e.tick())
    assert out["opened"] == [KEY]
    p = e.state["positions"][KEY]
    assert p["status"] == "open" and p["entry"] == 100.1 and p["ai_prob"] == 0.7
    assert p["risk_usdt"] <= 10_000 * 0.005 + 1e-6
    assert adapter.sent[0].id == client_id("live", KEY, SIG, "entry")
    run(e.tick())
    assert len(adapter.sent) == 1  # no second buy for the same signal
    _paper(paper, {}, [{"key": KEY, "signal_ts": SIG, "entry": 100.0, "exit": 104.0, "r": 0.8}])
    adapter.bid = 104.0
    out = run(e.tick())
    assert out["closed"] == [KEY] and not e.state["positions"]
    row = e.state["closed"][0]
    assert row["pnl_usdt"] > 0 and row["paper_r"] == 0.8 and "slip_entry_bps" in row
    kinds = [x["kind"] for x in events.read()]
    assert "trade_open" in kinds and "trade_close" in kinds


def test_executor_exits_at_stop_from_live_price(ex):
    make, adapter, *_ = ex
    e = make()
    run(e.tick())
    adapter.bid = 94.0
    out = run(e.tick())
    assert out["closed"] == [KEY] and e.state["closed"][0]["r"] < 0
    # the ask is still above the stop and the paper twin is still open: never buy that signal again
    assert not e.state["positions"] and len(adapter.sent) == 2
    run(e.tick())
    assert not e.state["positions"] and len(adapter.sent) == 2


def test_executor_rejected_and_partial_fills(ex):
    make, adapter, *_ = ex
    adapter.mode = "reject"
    e = make()
    run(e.tick())
    assert KEY not in e.state["positions"]
    run(e.tick())
    assert len(adapter.sent) == 1  # a rejected signal is not retried every tick
    adapter.mode, adapter.orders = "partial", {}
    e2 = make()
    e2.state["skipped"] = {}
    e2.state["positions"] = {}
    run(e2.tick())
    p = e2.state["positions"][KEY]
    assert p["status"] == "open" and any("partial" in n for n in p["notes"])


def test_executor_timeout_is_reconciled_without_double_buy(ex):
    make, adapter, *_ = ex
    adapter.mode = "timeout"
    e = make()
    run(e.tick())
    assert e.state["positions"][KEY]["status"] == "pending_entry"
    adapter.mode = "fill"
    e2 = make()  # a restart reads the saved state
    run(e2.tick())
    assert e2.state["positions"][KEY]["status"] == "open"
    assert len(adapter.sent) == 1


def test_executor_drops_an_order_that_never_arrived(ex):
    make, adapter, *_ = ex
    adapter.mode = "lose"
    e = make()
    run(e.tick())
    adapter.mode = "fill"
    run(e.tick())  # reconcile: not on the exchange -> dropped, then entered once
    assert e.state["positions"][KEY]["status"] == "open"


def test_executor_kill_switch_flattens(ex, tmp_path):
    make, adapter, _paper_path, events = ex
    e = make()
    run(e.tick())
    e.control.kill(by="test")
    out = run(e.tick())
    assert out["closed"] == [KEY] and e.state["flattened"]
    assert adapter.cash.get("BTC", 0) == pytest.approx(0, abs=1e-9)
    assert any(x["kind"] == "kill" and x["service"] == "executor-live" for x in events.read())
    run(e.tick())
    assert len(adapter.sent) == 2  # nothing new while killed


def test_executor_pause_blocks_entries(ex):
    make, adapter, *_ = ex
    e = make()
    e.control.pause("test")
    assert run(e.tick())["opened"] == [] and not adapter.sent


def test_executor_skips_old_and_stale_signals(ex):
    make, adapter, paper, events = ex
    _paper(paper, {KEY: _pos(SIG - 5 * HOUR)})
    e = make()
    assert run(e.tick())["skipped"] == [KEY]
    os.utime(paper, (NOW - 3600, NOW - 3600))  # paper service stopped writing
    e2 = make()
    e2.state["skipped"] = {}
    run(e2.tick())
    assert not adapter.sent
    assert any(x["kind"] == "disconnect" for x in events.read())


def test_executor_disconnect_alert_after_failures(ex):
    make, adapter, _p, events = ex
    adapter.fail = ConnectionError("down")
    e = make()
    for _ in range(3):
        run(e.tick())
    assert e.state["disconnected"]
    adapter.fail = None
    run(e.tick())
    kinds = [x["kind"] for x in events.read()]
    assert "disconnect" in kinds and "reconnect" in kinds


def test_executor_loss_cap_pauses(ex):
    make, adapter, *_ = ex
    e = make()
    e.state["closed"] = [{"exit_ts": int(NOW * 1000), "pnl_usdt": -400, "r": -1}]
    run(e.tick())
    assert e.control.paused and "loss cap" in e.control.state()["reason"]


def test_executor_pilot_cap_and_live_guard(ex):
    make, adapter, *_ = ex
    with pytest.raises(ValueError):
        make(live_guard=None)  # no guard
    with pytest.raises(ValueError):
        make(mode="testnet")  # demo trading is gone
    e = make(mode="live", live_guard=lambda sid: ["not approved"], pilot_max_usdt=500)
    assert run(e.tick())["skipped"] == [KEY] and not adapter.sent
    e2 = make(mode="live", live_guard=lambda sid: [], pilot_max_usdt=500)
    e2.state["skipped"] = {}
    run(e2.tick())
    assert adapter.sent and adapter.sent[0].filled * 100.1 <= 500 * 0.25 + 1e-6
    assert e2.state["last_equity"] == 500


# ---- go-live gate -----------------------------------------------------------------------------


def _gate_world(tmp_path, n_trades=120, r=0.3, oos=0.3):
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="s1", kind="rule", name="s1", status="paper", spec={"a": 1},
                  backtest={"out_of_sample": {"trades": 80, "expectancy_r": oos}}))
    reg.save()
    trades = [{"entry_id": "s1", "key": KEY, "exit_ts": i, "r": r if i % 2 else -0.2 + r}
              for i in range(n_trades)]
    with open(tmp_path / "paper.json", "w") as fh:
        json.dump({"open": {}, "trades": trades}, fh)
    events = EventLog(str(tmp_path / "ev.jsonl"), service="paper")
    gl = GoLive(str(tmp_path / "golive"), str(tmp_path / "reg.json"), str(tmp_path / "paper.json"),
                str(tmp_path / "models"), events=events, cfg=GateConfig(stable_days=1), clock=lambda: NOW)
    return gl, events, reg


def _beats(events, hours=30, every_min=10):
    with open(events.path, "a") as fh:
        start = int(NOW * 1000) - hours * HOUR
        for t in range(start, int(NOW * 1000), every_min * 60_000):
            fh.write(json.dumps({"ts": t, "service": "paper", "kind": "heartbeat", "text": "", "data": {}}) + "\n")


def test_gate_fails_until_everything_is_in_place(tmp_path):
    gl, events, _ = _gate_world(tmp_path)
    rep = gl.evaluate()
    failed = {c["name"] for c in rep["criteria"] if not c["passed"]}
    assert not rep["passed"]
    assert {"Stability", "Security: keys", "Security: kill switch tested"} <= failed
    with pytest.raises(ValueError):
        gl.approve(["s1"], by="Shawn")
    _beats(events)
    gl.attest("keys", by="Shawn")
    EventLog(events.path, service="executor-testnet").emit("kill", "flattened")  # old demo runs don't count
    assert not gl.evaluate()["passed"]
    gl.attest("kill_switch", by="Shawn", note="pulled it with nothing open")
    rep = gl.evaluate()
    assert rep["passed"], [c for c in rep["criteria"] if not c["passed"]]
    assert gl.guard("s1") == ["no go-live approval (aitrader golive approve)"]
    gl.approve(["s1"], by="Shawn")
    assert gl.guard("s1") == []
    assert gl.guard("other")


def test_gate_catches_small_samples_and_gaps(tmp_path):
    gl, events, _ = _gate_world(tmp_path, n_trades=40, r=0.6, oos=0.1)
    _beats(events, every_min=45)
    rep = {c["name"]: c["passed"] for c in gl.evaluate()["criteria"]}
    assert not rep["Paper trades completed"] and not rep["Paper vs backtest gap"] and not rep["Stability"]


def test_approval_breaks_when_rules_change_and_monitor(tmp_path):
    gl, events, reg = _gate_world(tmp_path)
    _beats(events)
    gl.attest("kill_switch", by="Shawn")
    gl.attest("keys", by="Shawn")
    gl.approve(["s1"], by="Shawn")
    with pytest.raises(ValueError):
        gl.approve(["a", "b", "c"], by="Shawn")
    reg = Registry(str(tmp_path / "reg.json"))
    reg.entries["s1"].spec = {"a": 2}
    reg.save()
    assert "changed" in gl.guard("s1")[0]
    assert gl.monitor([{"mode": "live", "r": -1.0}] * 11)  # drawdown past 10R
    assert gl.monitor([{"mode": "live", "r": 0.25}] * 25) is None
    assert "below the paper" in gl.monitor([{"mode": "live", "r": -0.1}] * 25)
    gl.revoke(by="Shawn")
    assert gl.guard("s1")


# ---- Telegram ---------------------------------------------------------------------------------


class FakeOpener:
    def __init__(self, updates=None):
        self.calls, self.updates = [], updates or []

    def __call__(self, req, timeout=None):
        import io
        import urllib.parse

        method = req.full_url.rsplit("/", 1)[1]
        params = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.calls.append((method, params))
        result = self.updates if method == "getUpdates" else {}
        self.updates = [] if method == "getUpdates" else self.updates

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps({"ok": True, "result": result}).encode())


def test_telegram_alerts_and_commands(tmp_path):
    events = EventLog(str(tmp_path / "ev.jsonl"), service="executor-live")
    events.emit("trade_open", "old event")  # before the service started: not replayed
    control = Control(str(tmp_path / "c.json"))
    opener = FakeOpener()
    bot = Telegram("T", "42", opener=opener)
    svc = TelegramService(bot, events, control, status_fn=lambda: "all good")
    assert svc.pump_alerts() == 0
    events.emit("trade_open", "live BUY")
    EventLog(events.path, service="paper").emit("trade_open", "paper BUY")  # too frequent: not sent
    events.emit("heartbeat", "alive")
    events.emit("risk_pause", "paused: daily loss")
    assert svc.pump_alerts() == 2
    sent = [p["text"] for m, p in opener.calls if m == "sendMessage"]
    assert "live BUY" in sent[0] and "daily loss" in sent[1]

    def msg(text, chat=42, uid=1):
        return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text, "from": {"username": "shawn"}}}

    assert svc.handle(msg("/pause", chat=7)) is None  # other chats are ignored
    assert not control.paused
    svc.handle(msg("/pause"))
    assert control.paused
    assert "CONFIRM" in svc.handle(msg("/kill")) and not control.killed
    svc.handle(msg("/kill CONFIRM"))
    assert control.killed
    assert svc.handle(msg("/status")) == "all good"
    opener.updates = [msg("/resume", uid=10)]
    svc.poll_commands(0)
    assert not control.paused and svc._offset_updates == 11


def test_telegram_needs_settings():
    with pytest.raises(ValueError):
        Telegram("", "")


# ---- reports ----------------------------------------------------------------------------------


def test_weekly_report_flags_gap(tmp_path):
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="good", kind="rule", name="g", status="paper",
                  backtest={"out_of_sample": {"trades": 50, "expectancy_r": 0.3}}))
    reg.add(Entry(id="off", kind="rule", name="o", status="paper",
                  backtest={"out_of_sample": {"trades": 50, "expectancy_r": 0.5}}))
    reg.save()
    now_ms = int(NOW * 1000)
    trades = ([{"entry_id": "good", "exit_ts": now_ms - HOUR, "r": 0.3, "pnl_usdt": 3}] * 25
              + [{"entry_id": "off", "exit_ts": now_ms - HOUR, "r": 0.0, "pnl_usdt": 0}] * 25)
    with open(tmp_path / "paper.json", "w") as fh:
        json.dump({"open": {}, "trades": trades}, fh)
    with open(tmp_path / "live.json", "w") as fh:
        json.dump({"positions": {}, "closed": [{"exit_ts": now_ms, "r": 0.2, "paper_r": 0.3, "pnl_usdt": 2,
                                                "slip_entry_bps": 4, "slip_exit_bps": 3}]}, fh)
    rep = weekly(str(tmp_path / "reg.json"), str(tmp_path / "paper.json"),
                 {"live": str(tmp_path / "live.json")}, now=NOW)
    assert rep["flagged"] == ["off"]
    assert rep["executors"]["live"]["vs_paper_r"] == pytest.approx(-0.1)
    text = weekly_text(rep)
    assert "CHECK" in text and "live" in text
    day = datetime.fromtimestamp(NOW, timezone.utc).date()
    assert "50 closed" in daily_text(day, str(tmp_path / "paper.json"), {})


def test_status_text(tmp_path):
    from aitrader.config import Settings

    c = Control(str(tmp_path / "c.json"))
    c.pause("test")
    text = status_text(Settings(), c, str(tmp_path / "none.json"), {})
    assert "PAUSED" in text and "locked" in text


# ---- paper trader honours pause ---------------------------------------------------------------


def test_cli_kill_needs_confirm(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["--env-file", "none", "trade", "kill"]) == 1
    assert not Control("data/trade/control.json").killed
    assert main(["--env-file", "none", "trade", "kill", "--confirm", "--by", "t"]) == 0
    assert Control("data/trade/control.json").killed
    assert main(["--env-file", "none", "trade", "resume"]) == 0
    assert main(["--env-file", "none", "trade", "status"]) == 0
    assert "running" in capsys.readouterr().out
    # live refuses while the live lock is closed, before any exchange is touched
    with pytest.raises(Exception):
        main(["--env-file", "none", "trade", "run", "--mode", "live", "--once"])
    with pytest.raises(SystemExit):  # demo trading is gone
        main(["--env-file", "none", "trade", "run", "--mode", "testnet", "--once"])


def test_ccxt_adapter_client_ids_and_lock(monkeypatch):
    from aitrader.config import LIVE_CONFIRM_PHRASE, Mode, Settings
    from aitrader.exchanges import BybitAdapter

    class OrderNotFound(Exception):
        pass

    class BadRequest(Exception):
        pass

    class Client:
        markets = {"BTC/USDT": {}}

        def __init__(self):
            self.calls = []

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.5f}"

        async def create_order(self, *args):
            self.calls.append(args)
            return {"id": "1", "symbol": args[0], "side": args[2], "type": args[1], "amount": args[3],
                    "status": "closed", "filled": args[3], "average": 100.0, "timestamp": 1}

        def market(self, symbol):
            return {"id": symbol.replace("/", ""), "symbol": symbol}

        async def private_get_v5_order_realtime(self, req):
            self.looked_up = req
            if req["orderLinkId"] == "bad":
                raise BadRequest()
            return {"result": {"list": []}}

        async def private_get_v5_order_history(self, req):
            return {"result": {"list": []}}

    client = Client()
    ad = BybitAdapter(Settings(mode=Mode.LIVE, live_unlocked=True, live_confirm=LIVE_CONFIRM_PHRASE), client=client)
    run(ad.place_order("BTC/USDT", Side.BUY, 0.1234567, client_id="ait123"))
    assert client.calls[0][3] == pytest.approx(0.12346)
    assert client.calls[0][5] == {"isLeverage": 0, "marketUnit": "baseCoin", "orderLinkId": "ait123"}
    assert run(ad.find_order("ait123", "BTC/USDT")) is None
    assert client.looked_up == {"category": "spot", "symbol": "BTCUSDT", "orderLinkId": "ait123"}
    with pytest.raises(BadRequest):  # an error is not "the order never arrived"
        run(ad.find_order("bad", "BTC/USDT"))
    live = BybitAdapter(Settings(), client=Client())  # paper mode, live lock closed
    with pytest.raises(Exception):
        run(live.place_order("BTC/USDT", Side.BUY, 1, client_id="x"))


def test_executor_skips_pairs_the_exchange_does_not_list(ex):
    make, adapter, paper, events = ex
    pepe = "s1|bybit|PEPE/USDT|1h"
    _paper(paper, {pepe: _pos(SIG), KEY: _pos(SIG)})

    class BadSymbol(Exception):
        pass

    real = adapter.get_orderbook

    async def book(symbol, depth=20):
        if symbol == "PEPE/USDT":
            raise BadSymbol("bybit does not have market symbol PEPE/USDT")
        return await real(symbol, depth)

    adapter.get_orderbook = book
    out = run(make().tick())
    assert out["skipped"] == [pepe] and out["opened"] == [KEY]
    assert any("isn't listed" in x["text"] for x in events.read(kinds={"signal_skipped"}))


def test_gate_leaves_out_strategies_that_only_paper_trade(tmp_path):
    gl, _events, reg = _gate_world(tmp_path)
    reg.add(Entry(id="untested", kind="rule", name="u", status="paper", spec={"a": 2}))
    reg.add(Entry(id="failed", kind="rule", name="f", status="paper", spec={"a": 3},
                  backtest={"out_of_sample": {"trades": 80, "expectancy_r": -0.2}, "failed": ["expectancy"]}))
    reg.save()
    assert gl.evaluate()["strategies"] == ["s1"]  # paper-all strategies are not live candidates
    rep = gl.evaluate(["failed"])  # named on purpose: the backtest line still fails it
    assert not next(c for c in rep["criteria"] if c["name"].startswith("Expectancy after costs (backtest"))["passed"]


def test_executor_leaves_dust_and_still_finishes_the_kill_switch(ex):
    make, adapter, *_ = ex
    e = make()
    run(e.tick())
    p = e.state["positions"][KEY]
    adapter.cash["BTC"] = p["amount"] = 0.00001  # what is left after fees and rounding

    async def sellable(symbol, amount, price):
        return 0.0 if amount * price < 5 else amount

    adapter.sellable = sellable
    e.control.kill(by="test")
    run(e.tick())
    assert e.state["flattened"] and e.state["positions"][KEY]["status"] == "dust"
    assert not [o for o in adapter.sent if o.side is Side.SELL]


def test_executor_values_a_bnb_fee_at_the_bnb_price(ex):
    make, adapter, *_ = ex
    e = make()
    order = Order("x", "bybit", "BTC/USDT", Side.SELL, OrderType.MARKET, 1, None, OrderStatus.FILLED,
                  filled=1, average=60_000, fee=0.01, fee_currency="BNB")
    adapter.bid = 600.0  # the book answers every symbol, BNB/USDT included
    assert run(e._fee_in_quote(order, 60_000)) == pytest.approx(6.0)  # not 0.01 * 60,000


def test_gate_needs_every_strategy_to_stand_on_its_own(tmp_path):
    gl, events, reg = _gate_world(tmp_path)
    reg.add(Entry(id="s2", kind="rule", name="s2", status="paper", spec={"a": 9},
                  backtest={"out_of_sample": {"trades": 80, "expectancy_r": 0.3}}))
    reg.save()
    rep = gl.evaluate(["s1", "s2"])  # s2 has no paper trades: s1's good results can't carry it
    row = next(c for c in rep["criteria"] if c["name"].startswith("Every strategy profitable"))
    assert not row["passed"] and "s2" in row["value"]


# ---- resting stops on the exchange --------------------------------------------------------------


class StopAdapter(FakeAdapter):
    """FakeAdapter plus stop orders that rest on the exchange until the price falls to them."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.stops: dict[str, Order] = {}
        self.cancels: list[str] = []

    async def place_stop(self, symbol, amount, trigger_price, client_id, reason=""):
        o = Order(client_id, "bybit", symbol, Side.SELL, OrderType.MARKET, amount, trigger_price,
                  OrderStatus.OPEN)
        self.stops[client_id] = self.orders[client_id] = o
        return o

    async def cancel_stop(self, client_id, symbol):
        o = self.stops.get(client_id)
        if o is None or o.status is not OrderStatus.OPEN:
            return False
        self.cancels.append(client_id)
        o.status = OrderStatus.CANCELED
        return True

    def fire(self, bid):
        """The price falls through the stops: Bybit sells while the executor isn't looking."""
        self.bid = bid
        for o in self.stops.values():
            if o.status is OrderStatus.OPEN and bid <= o.price:
                base = o.symbol.split("/")[0]
                o.status, o.filled, o.average = OrderStatus.FILLED, o.amount, bid
                o.fee, o.fee_currency = o.amount * bid * 0.001, "USDT"
                self.cash[base] -= o.amount
                self.cash["USDT"] += o.amount * bid - o.fee


@pytest.fixture
def sx(ex):
    make, _adapter, paper, events = ex
    adapter = StopAdapter()

    def make2(**kw):
        e = make(**kw)
        e.adapter = adapter
        return e

    return make2, adapter, paper, events


def test_every_open_position_gets_a_stop_resting_on_the_exchange(sx):
    make, adapter, paper, events = sx
    e = make()
    run(e.tick())
    p = e.state["positions"][KEY]
    stop = adapter.stops[p["stop_cid"]]
    assert p["stop_armed"] and stop.price == 95.0 and stop.amount == p["amount"]
    run(e.tick())
    assert len(adapter.stops) == 1  # armed once, not every tick
    assert any(x["kind"] == "stop_armed" for x in events.read())


def test_the_resting_stop_is_cancelled_before_the_executor_sells(sx):
    make, adapter, paper, _events = sx
    e = make()
    run(e.tick())
    cid = e.state["positions"][KEY]["stop_cid"]
    _paper(paper, {}, [{"key": KEY, "signal_ts": SIG, "entry": 100.0, "exit": 104.0, "r": 0.8}])
    adapter.bid = 104.0
    out = run(e.tick())
    assert out["closed"] == [KEY] and adapter.cancels == [cid]
    assert adapter.stops[cid].status is OrderStatus.CANCELED  # nothing left to sell another position's coins


def test_a_stop_that_fired_on_the_exchange_is_booked_once(sx):
    make, adapter, *_ = sx
    e = make()
    run(e.tick())
    adapter.fire(94.0)
    out = run(e.tick())
    assert out["closed"] == [KEY] and not e.state["positions"]
    row = e.state["closed"][0]
    assert row["exit_reason"] == "stop on bybit" and row["exit"] == 94.0 and row["r"] < 0
    assert not [o for o in adapter.sent if o.side is Side.SELL]  # Bybit sold; the executor didn't sell again


def test_a_stop_that_fired_while_the_server_was_down(sx):
    make, adapter, *_ = sx
    run(make().tick())
    adapter.fire(94.0)
    adapter.bid = 96.0  # bounced back above the stop before the restart
    e = make()
    e.state["checked"] = False  # a restart checks holdings first
    out = run(e.tick())
    assert out["closed"] == [KEY] and e.state["closed"][0]["exit_reason"] == "stop on bybit"


def test_a_stop_whose_answer_was_lost_is_found_not_doubled(sx):
    make, adapter, *_ = sx
    real = adapter.place_stop

    async def lost(*a, **k):
        await real(*a, **k)
        raise TimeoutError("lost")

    adapter.place_stop = lost
    e = make()
    run(e.tick())
    assert not e.state["positions"][KEY]["stop_armed"]
    adapter.place_stop = real
    run(e.tick())
    assert e.state["positions"][KEY]["stop_armed"] and len(adapter.stops) == 1


def test_kill_switch_cancels_resting_stops_and_sells(sx):
    make, adapter, *_ = sx
    e = make()
    run(e.tick())
    e.control.kill(by="test")
    out = run(e.tick())
    assert out["closed"] == [KEY] and e.state["flattened"] and len(adapter.cancels) == 1
    assert all(o.status is not OrderStatus.OPEN for o in adapter.stops.values())


# ---- streamed prices (Bybit WebSocket) ----------------------------------------------------------


class FakePrices:
    def __init__(self, symbols=("BTC/USDT",)):
        self.symbols, self.bid, self.age = list(symbols), {}, {}

    def stale(self, symbol, max_age):
        return self.age.get(symbol, float("inf")) > max_age

    def best_bid(self, symbol):
        return self.bid.get(symbol)


def test_streamed_price_triggers_the_stop_and_quiet_stream_blocks_entries(ex):
    make, adapter, *_ = ex
    prices = FakePrices()
    e = make(prices=prices)
    assert run(e.tick())["opened"] == []  # BTC is streamed but nothing has arrived: wait
    assert not adapter.sent and not e.state["skipped"]  # waiting, not skipped for good
    prices.age["BTC/USDT"], prices.bid["BTC/USDT"] = 1.0, 100.0
    assert run(e.tick())["opened"] == [KEY]
    assert not e._stop_hit()
    prices.bid["BTC/USDT"] = 94.0  # the order book still says 100: the stream is ahead
    assert e._stop_hit()
    out = run(e.tick())
    assert out["closed"] == [KEY]
