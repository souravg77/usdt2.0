"""Phase 1 safety: secrets never reach logs or alerts, and the executor halts itself when it
can't vouch for a position (no resting stop, account vs ledger mismatch, quiet price feed,
repeated failures)."""

import io
import logging

import pytest

from aitrader.models import Order, OrderStatus, OrderType, Side
from aitrader.ops import EventLog
from aitrader.ops.redact import MASK, RedactingFormatter, install, redact
from aitrader.trade import executor as executor_mod

from test_trade import KEY, NOW, FakePrices, StopAdapter, run
from test_web import login, web  # noqa: F401  (fixture)

SIGNED = ("bybit GET https://api.bybit.com/v5/order/realtime?category=spot&orderLinkId=ait1"
          "&sign=9f86d081884c7d659a2feaa0c55ad015 400 {\"retMsg\":\"error\"}")


# ---- redaction ----------------------------------------------------------------------------------


def test_redact_hides_keys_signatures_and_signed_urls(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "AbCdEfGh12345678")
    monkeypatch.setenv("BYBIT_API_SECRET", "s3cr3tVALUEs3cr3tVALUE")
    text = redact(SIGNED + " key AbCdEfGh12345678 secret s3cr3tVALUEs3cr3tVALUE")
    assert "AbCdEfGh12345678" not in text and "s3cr3tVALUE" not in text
    assert "9f86d081" not in text and "https://api.bybit.com/v5/order/realtime?" + MASK in text
    headers = redact("{'X-BAPI-API-KEY': 'k123456789', 'X-BAPI-SIGN': 'abcdef0123456789', 'X-BAPI-TIMESTAMP': '1'}")
    assert "k123456789" not in headers and "abcdef0123456789" not in headers
    assert "abcdef" not in redact("apiKey=abcdef123&recv=5000") and "zzzz" not in redact('"password": "zzzzzz"')
    assert MASK in redact("Authorization: Bearer AAAAAAAAAAAAAAAA") and "sk-ant-api03" not in redact("sk-ant-api03-xyzxyzxyz")
    assert "1234567:" not in redact("https://api.telegram.org/bot1234567:ABCdefGHIjklMNOpqrSTUvwx/sendMessage")
    assert redact("BUY 0.01 BTC/USDT @ 62000 (s1, stop 61000)") == "BUY 0.01 BTC/USDT @ 62000 (s1, stop 61000)"


def test_logs_and_tracebacks_are_redacted(monkeypatch):
    monkeypatch.setenv("BYBIT_API_SECRET", "s3cr3tVALUEs3cr3tVALUE")
    old_factory = logging.getLogRecordFactory()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("aitrader.test_redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        install(logger)
        assert isinstance(handler.formatter, RedactingFormatter)
        logger.warning("tick failed: %s", SIGNED)
        try:
            raise RuntimeError("auth failed for s3cr3tVALUEs3cr3tVALUE at " + SIGNED)
        except RuntimeError:
            logger.exception("crash")
        out = stream.getvalue()
        assert "9f86d081" not in out and "s3cr3tVALUE" not in out
        assert "RuntimeError" in out and MASK in out  # the traceback is kept, minus the secrets
    finally:
        logging.setLogRecordFactory(old_factory)
        logger.removeHandler(handler)


def test_event_log_and_alerts_never_carry_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:telegramTOKENvalue")
    ev = EventLog(str(tmp_path / "ev.jsonl"), service="executor-live")
    ev.emit("error", "live executor: " + SIGNED, level="error", detail={"url": SIGNED, "tok": "123456:telegramTOKENvalue"})
    raw = (tmp_path / "ev.jsonl").read_text()
    assert "9f86d081" not in raw and "telegramTOKENvalue" not in raw and "orderLinkId" not in raw


# ---- safety halt --------------------------------------------------------------------------------


@pytest.fixture
def sx(tmp_path):
    from test_trade import _paper, _pos, SIG
    from aitrader.trade import Control, Executor

    paper = tmp_path / "paper.json"
    _paper(paper, {KEY: _pos(SIG)})
    adapter = StopAdapter()
    events = EventLog(str(tmp_path / "ev.jsonl"), service="executor-live")
    clock = [NOW]

    def make(**kw):
        return Executor(adapter, "live", "bybit", str(paper), state_path=str(tmp_path / "state.json"),
                        control=Control(str(tmp_path / "c.json")), events=events,
                        clock=lambda: clock[0], live_guard=lambda sid: [], **kw)

    return make, adapter, paper, events, clock


def halts(events):
    return [x for x in events.read() if x["kind"] == "halt"]


def test_account_not_matching_the_ledger_halts_new_entries(sx):
    make, adapter, paper, events, clock = sx
    e = make()
    run(e.tick())
    assert e.state["positions"][KEY]["status"] == "open" and not halts(events)
    adapter.cash["BTC"] = 0.0  # someone sold the coins by hand on Bybit
    clock[0] += executor_mod.RECHECK_S
    out = run(e.tick())
    assert e.control.halted and e.control.paused and "doesn't match the ledger" in e.control.state()["reason"]
    assert out["halted"] and len(halts(events)) == 1 and halts(events)[0]["level"] == "error"
    assert e.state["positions"][KEY]["status"] == "missing"
    stop_cid = e.state["positions"][KEY]["stop_cid"]
    assert adapter.stops[stop_cid].status is OrderStatus.OPEN  # protective stops are left alone
    clock[0] += executor_mod.RECHECK_S
    run(e.tick())
    assert len(halts(events)) == 1  # one alert, not one per pass
    e.control.resume(by="admin")
    clock[0] += executor_mod.RECHECK_S
    run(e.tick())
    assert not e.control.paused  # resuming isn't undone by the same old mismatch


def test_a_position_without_a_resting_stop_halts(sx):
    make, adapter, paper, events, clock = sx

    async def refuse(*a, **kw):
        return Order(kw.get("client_id") or a[3], "bybit", a[0], Side.SELL, OrderType.MARKET, a[1], a[2],
                     OrderStatus.REJECTED, reason="trigger price invalid")

    adapter.place_stop = refuse
    e = make()
    run(e.tick())
    assert not e.control.paused  # the grace period: Bybit may just be slow
    clock[0] += executor_mod.STOP_GRACE_S
    run(e.tick())
    assert e.control.halted and "no confirmed stop" in e.control.state()["reason"]
    assert len(halts(events)) == 1


def test_a_quiet_price_feed_halts_once_per_quiet_spell(sx, monkeypatch):
    make, adapter, paper, events, clock = sx
    prices = FakePrices()
    prices.age["BTC/USDT"], prices.bid["BTC/USDT"] = 1.0, 100.0
    e = make(prices=prices)
    run(e.tick())
    assert e.state["positions"][KEY]["status"] == "open"
    prices.age["BTC/USDT"] = executor_mod.feed_halt_s() + 1
    run(e.tick())
    assert e.control.halted and "price stream" in e.control.state()["reason"]
    e.control.resume(by="admin")
    run(e.tick())
    assert not e.control.paused  # still the same quiet spell: no second halt
    prices.age["BTC/USDT"] = 1.0
    run(e.tick())
    prices.age["BTC/USDT"] = 500.0
    run(e.tick())
    assert e.control.halted and len(halts(events)) == 2
    monkeypatch.setenv("AITRADER_FEED_HALT_S", "5")
    assert executor_mod.feed_halt_s() == 30.0  # never below 30 seconds


def test_halt_cancels_entry_orders_still_waiting(sx):
    make, adapter, paper, events, clock = sx
    e = make()
    cid = "ait-waiting"
    e.state["positions"]["w|bybit|ETH/USDT|1h"] = {
        "key": "w|bybit|ETH/USDT|1h", "entry_id": "w", "symbol": "ETH/USDT", "status": "pending_entry",
        "entry_cid": cid, "signal_ts": 1, "stop": 1.0}
    adapter.orders[cid] = Order(cid, "bybit", "ETH/USDT", Side.BUY, OrderType.LIMIT, 1.0, 2000.0, OrderStatus.OPEN)
    cancelled = []

    async def cancel_order(order_id, symbol):
        cancelled.append(order_id)

    adapter.cancel_order = cancel_order
    assert run(e._cancel_waiting_entries()) == 1 and cancelled == [cid]


def test_repeated_failures_halt(sx):
    make, adapter, paper, events, clock = sx
    adapter.fail = ValueError("bybit says retCode 10002")
    e = make()
    for _ in range(executor_mod.HALT_AFTER_FAILURES):
        run(e.tick())
    assert e.control.halted and "in a row failed" in e.control.state()["reason"]
    assert len(halts(events)) == 1


def test_resume_clears_the_halt(tmp_path):
    from aitrader.trade import Control

    c = Control(str(tmp_path / "c.json"))
    c.halt("feed quiet", by="safety halt (live)")
    assert c.paused and c.halted and not c.killed
    c.halt("second reason")
    assert c.state()["reason"] == "feed quiet"  # the first reason is kept
    c.kill("kill", by="admin")
    c.halt("later")
    assert c.killed  # a halt never downgrades a kill
    c.resume(by="admin")
    assert not (c.paused or c.halted or c.killed)


def test_dashboard_shows_the_safety_halt(web):
    from aitrader.trade import Control

    client, *_ = web
    login(client)
    Control("data/trade/control.json").halt("BTC/USDT (ledger 0.01, account 0)", by="safety halt (live)")
    r = client.get("/trading")
    assert "SAFETY HALT" in r.text and "ledger 0.01" in r.text and "Safety halt" in r.text
