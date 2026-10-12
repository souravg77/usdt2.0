"""Phase 6: the Monitor page and its JSON endpoints (feed health, derivatives, signals and
abstentions, risk and exposure with the kill switch)."""

import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.data.bybit_ws import BybitSpotStream
from aitrader.data.store import CandleStore
from aitrader.models import Candle
from aitrader.ops import feedstats
from aitrader.trade.risk import RiskLimits
from aitrader.web.app import Paths, create_app
from aitrader.web.auth import Auth
from aitrader.web.jobs import JobRunner
from aitrader.web.monitor_pages import SignalBoards, risk_view, value_at_risk

from test_strategies import synthetic
from test_web import PW, login

HOUR = 3_600_000
NOW = 1_760_000_000.0


class FakePerp:
    def __init__(self):
        self.calls = 0

    async def snapshot(self, symbols):
        self.calls += 1
        return {"source": "live", "rows": [{"symbol": "BTC/USDT", "last": 101.0, "mark": 100.5, "index": 100.0,
                                            "basis": 0.01, "mark_premium": 0.005, "funding_rate": 0.0001,
                                            "funding_annualized": 0.1095, "next_funding_ts": None,
                                            "open_interest": 5.0, "ts": 1, "funding_in_h": 2.5}]}


def _store(db, symbols=("BTC/USDT", "ETH/USDT"), n=700, gap=False):
    store = CandleStore(str(db))
    end = int(NOW * 1000) // HOUR * HOUR
    for k, sym in enumerate(symbols):
        df = synthetic(n=n, seed=k + 1)
        df["ts"] = end - (n - np.arange(n)) * HOUR
        df["volume"] *= 1e5
        rows = [Candle("bybit", sym, "1h", int(r.ts), r.open, r.high, r.low, r.close, r.volume)
                for i, r in enumerate(df.itertuples()) if not (gap and i == n - 50)]
        store.upsert(rows)
    store.close()


@pytest.fixture
def mon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "c.db"
    _store(db, gap=True)
    settings = Settings(db_path=str(db), symbols=["BTC/USDT", "ETH/USDT"], timeframes=["1h"])
    paths = Paths(trade_dir=str(tmp_path / "trade"), feed=str(tmp_path / "feed.json"),
                  paper_state=str(tmp_path / "paper.json"), events=str(tmp_path / "ev.jsonl"))
    perp = FakePerp()
    app = create_app(settings, Auth("admin", PW, "k"), JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x")),
                     paths, monitor={"perp": perp, "boards": SignalBoards(str(db)), "clock": lambda: NOW})
    return TestClient(app), paths, perp, db


def test_monitor_needs_login(mon):
    client, *_ = mon
    assert client.get("/monitor", follow_redirects=False).headers["location"] == "/login"
    for api in ("feed", "derivatives", "signals", "risk"):
        assert client.get(f"/api/monitor/{api}").status_code == 401


def test_feed_health_from_the_stream_status_and_the_candle_store(mon):
    client, paths, *_ = mon
    login(client)
    f = client.get("/api/monitor/feed").json()
    assert f["state"].startswith("not running")
    gaps = {(g["symbol"], g["timeframe"]): g for g in f["gaps"]}
    assert gaps[("BTC/USDT", "1h")]["missing"] == 1 and gaps[("ETH/USDT", "1h")]["missing"] == 1
    feedstats.save({"connected": True, "reconnects": 2, "latency_ms": 41.5, "last_error": "",
                    "ages": {"BTC/USDT": 0.4, "ETH/USDT": 22.0}, "history": [{"at": NOW - 5, "event": "reconnected"}]},
                   paths.feed, now=NOW - 3)
    f = client.get("/api/monitor/feed").json()
    assert f["state"] == "connected" and f["latency_ms"] == 41.5 and f["reconnects"] == 2
    assert f["quiet_pairs"] == ["ETH/USDT"] and f["history"][0]["event"] == "reconnected"
    feedstats.save({"connected": True}, paths.feed, now=NOW - 120)
    assert client.get("/api/monitor/feed").json()["state"].startswith("stale")


def test_stream_measures_ping_round_trip_and_logs_connections():
    clock = [100.0]
    s = BybitSpotStream(["BTC/USDT"], clock=lambda: clock[0])
    s._pings = {"7": 100.0}
    clock[0] = 100.045
    assert s._handle(json.dumps({"op": "ping", "success": True, "ret_msg": "pong", "req_id": "7"})) is False
    assert s.latency_ms == pytest.approx(45.0)
    s._note("connected")
    st = s.status()
    assert st["latency_ms"] == pytest.approx(45.0) and st["history"][0]["event"] == "connected"
    assert st["ages"] == {"BTC/USDT": None} and st["reconnects"] == 0


def test_derivatives_panel(mon):
    client, _paths, perp, _db = mon
    login(client)
    d = client.get("/api/monitor/derivatives").json()
    assert d["rows"][0]["mark"] == 100.5 and d["rows"][0]["basis"] == 0.01
    r = client.get("/monitor")
    assert r.status_code == 200 and "Mark" in r.text and "+1.000%" in r.text


def test_signal_and_abstention_stream(mon):
    client, *_ = mon
    login(client)
    data = client.get("/api/monitor/signals").json()
    assert data["timeframe"] == "1h" and [b["symbol"] for b in data["boards"]] == ["BTC/USDT", "ETH/USDT"]
    for b in data["boards"]:
        assert b["regime"] and len(b["candidates"]) + len(b["abstentions"]) >= 30
        assert all(a["reason"] for a in b["abstentions"])
    text = client.get("/monitor").text
    assert "Why the others abstained" in text


def test_signal_board_is_cached_until_a_candle_closes(tmp_path):
    db = tmp_path / "c.db"
    _store(db)
    boards = SignalBoards(str(db))
    first = boards.board("BTC/USDT", int(NOW * 1000))
    assert boards.board("BTC/USDT", int(NOW * 1000) + 60_000) is first
    assert boards.board("DOGE/USDT", int(NOW * 1000))["note"]


def _live_state(now):
    return {"last_equity": 10_000.0,
            "positions": {"a": {"symbol": "BTC/USDT", "status": "open", "side": "long", "notional": 2_000.0,
                                "stop": 95.0, "stop_armed": True, "risk_usdt": 25.0},
                          "b": {"symbol": "ETH/USDT", "status": "open", "side": "short", "notional": 1_500.0,
                                "leverage": 3.0, "stop": 105.0, "stop_armed": False},
                          "c": {"symbol": "SOL/USDT", "status": "pending_entry", "notional": 0}},
            "closed": [{"exit_ts": int(now * 1000) - 60_000, "pnl_usdt": -40.0},
                       {"exit_ts": int(now * 1000) - 3 * 86_400_000, "pnl_usdt": -500.0}]}


def test_risk_view_exposure_daily_cap_margin_and_var(tmp_path):
    db = tmp_path / "c.db"
    _store(db)
    r = risk_view(_live_state(NOW), {"open": {"x": {}}}, str(db), "bybit", RiskLimits(), NOW)
    assert [p["symbol"] for p in r["positions"]] == ["BTC/USDT", "ETH/USDT"]
    assert r["exposure_usdt"] == 3_500.0 and r["exposure_pct"] == pytest.approx(0.35)
    assert r["today_pnl"] == -40.0 and r["daily_cap_usdt"] == pytest.approx(100.4)
    assert r["daily_cap_used"] == pytest.approx(40 / 100.4, rel=1e-3)
    assert r["margin_usdt"] == 500.0 and r["margin_used"] == pytest.approx(0.05)
    assert r["var95_usdt"] > 0 and r["paper_open"] == 1 and r["limits"]["max_leverage"] == 3.0
    hedged = value_at_risk([{"symbol": "BTC/USDT", "notional": 1000.0}, {"symbol": "BTC/USDT", "notional": 1000.0,
                                                                        "short": True}], str(db), "bybit", int(NOW * 1000))
    assert hedged == pytest.approx(0.0, abs=1e-6)  # a long and an equal short cancel out
    assert value_at_risk([], str(db), "bybit", int(NOW * 1000)) == 0.0


def test_risk_panel_and_one_click_kill_switch(mon):
    client, paths, *_ = mon
    os.makedirs(paths.trade_dir, exist_ok=True)
    with open(os.path.join(paths.trade_dir, "live_state.json"), "w") as fh:
        json.dump(_live_state(NOW), fh)
    token = login(client)
    r = client.get("/api/monitor/risk").json()
    assert r["live"] and r["control"]["killed"] is False
    page = client.get("/monitor").text
    assert "Emergency kill switch" in page and 'action="/trading/kill"' in page and "<b class=bad>no</b>" in page
    csrf = Auth("admin", PW, "k").csrf(token)
    resp = client.post("/trading/kill", data={"csrf": csrf, "confirm": "KILL"}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/api/monitor/risk").json()["control"]["killed"] is True
