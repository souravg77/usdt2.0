import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.learn import Entry, Registry
from aitrader.web import Auth, AuthError, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"


def test_auth_rules():
    with pytest.raises(AuthError):
        Auth("admin", "short")
    a = Auth("admin", PW, "s3cret", max_failures=3)
    assert a.login("1.1.1.1", "admin", "nope") is None
    tok = a.login("1.1.1.1", "admin", PW, now=1000)
    assert a.verify(tok, now=1001) == "admin"
    assert a.verify(tok, now=1000 + a.session_s + 1) is None  # expired
    assert a.verify(tok[:-4] + "AAAA", now=1001) is None  # tampered
    assert Auth("admin", PW, "other").verify(tok, now=1001) is None  # other key
    assert a.check_csrf(tok, a.csrf(tok)) and not a.check_csrf(tok, "x" * 32)
    for _ in range(3):
        a.login("2.2.2.2", "admin", "wrong")
    assert a.locked_out("2.2.2.2")
    assert a.login("2.2.2.2", "admin", PW) is None  # right password, still locked out
    assert a.login("3.3.3.3", "admin", PW)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = Auth("admin", PW, "k")
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    paths = Paths(books="data/books", registry="data/learn/registry.json", models="data/ai/models",
                  paper_state="data/learn/paper_state.json", kb_index="data/kb/index.json")
    client = TestClient(create_app(Settings(), auth, runner, paths))
    return client, auth, runner, paths


def login(client):
    r = client.post("/login", data={"user": "admin", "password": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client.cookies.get(COOKIE)


def test_pages_need_login(web):
    client, *_ = web
    for path in ("/", "/books", "/jobs/abc", "/strategies/x"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = client.post("/login", data={"user": "admin", "password": "wrong"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    assert client.get("/healthz").text == "ok"


def test_overview_shows_lock_strategies_and_escapes(web):
    client, auth, _runner, paths = web
    reg = Registry(paths.registry)
    reg.add(Entry(id="evil<script>", kind="rule", name="x", source="<b>book</b>", status="paper"))
    reg.save()
    login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "locked" in r.text and "paper mode" in r.text
    assert "<script>" not in r.text and "evil&lt;script&gt;" in r.text
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "Off: every signal is taken" in r.text
    assert "PAPER" in r.text and "DEMO" not in r.text and "did not pass the backtest gate" in r.text  # ladder state


def test_submit_book_runs_the_pipeline(web, monkeypatch):
    client, auth, runner, paths = web
    token = login(client)
    files = {"book": ("../../evil name.md", b"# Book\n\n" + b"Buy when RSI crosses 30. " * 20, "text/markdown")}
    r = client.post("/books", files=files, data={"max": "5", "test": "1"}, follow_redirects=False)
    assert r.status_code == 403  # no CSRF token

    csrf = auth.csrf(token)
    r = client.post("/books", files=files, data={"max": "5", "test": "1", "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    job_id = r.headers["location"].rsplit("/", 1)[1]
    runner.wait_idle(120)
    job = runner.get(job_id)
    saved = job["meta"]["book"]
    assert saved.startswith("data/books/") and "/" not in saved[len("data/books/"):]
    assert re.search(r"evil-name\.md$", saved)
    # no API key: the book is indexed into the knowledge base only
    assert [s[0] for s in job["steps"]] == ["kb"]
    assert job["status"] == "done", runner.log_text(job_id)
    assert json.load(open("data/kb/index.json"))["documents"]
    page = client.get(f"/jobs/{job_id}").text
    assert "done" in page and "aitrader kb build" in page

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    r = client.post("/books", files=files, data={"max": "50", "test": "1", "csrf": csrf},
                    follow_redirects=False)
    job = runner.get(r.headers["location"].rsplit("/", 1)[1])
    read = job["steps"][1]
    assert read[:4] == ["learn", "--registry", paths.registry, "read"]
    assert read[-3:] == ["--max", "30", "--test"]


def test_rejects_bad_uploads(web):
    client, auth, runner, _paths = web
    csrf = auth.csrf(login(client))
    r = client.post("/books", files={"book": ("x.exe", b"MZ\x90\x00" * 100, "application/octet-stream")},
                    data={"csrf": csrf}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = client.post("/books", files={"book": ("x.txt", b"\xff\xfe\x00bad" * 100, "text/plain")},
                    data={"csrf": csrf}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert runner.list() == []


def test_logout(web):
    client, auth, *_ = web
    token = login(client)
    client.post("/logout", data={"csrf": auth.csrf(token)})
    assert client.get("/", follow_redirects=False).status_code == 303
    assert auth.verify(token) is None  # the old cookie no longer works either


def test_trading_page_and_controls(web):
    client, auth, _runner, paths = web
    assert client.get("/trading", follow_redirects=False).status_code == 303
    os.makedirs("data/learn", exist_ok=True)
    os.makedirs("data/trade", exist_ok=True)
    key = "s1|bybit|BTC/USDT|1h"
    with open(paths.paper_state, "w") as fh:
        json.dump({"open": {key: {"entry_id": "s1", "filled": True, "entry": 100.0, "mark_r": 0.4, "ai_prob": 0.66,
                                  "signal": {"ts": 1, "entry": 100.0, "stop": 95.0}}},
                   "trades": [{"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_000_000_000,
                               "entry": 100, "exit": 105, "r": 1.0, "pnl_usdt": 50, "exit_reason": "target",
                               "ai_prob": 0.71},
                              {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_100_000_000,
                               "entry": 100, "exit": 98, "r": -0.4, "pnl_usdt": -20, "exit_reason": "<stop>"}]}, fh)
    with open("data/trade/live_state.json", "w") as fh:
        json.dump({"positions": {key: {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "amount": 0.5,
                                       "entry": 100.1, "stop": 95.0, "status": "open"}},
                   "closed": [], "equity_curve": [[1, 10000], [2, 10050]]}, fh)
    token = login(client)
    r = client.get("/trading")
    assert r.status_code == 200
    for text in ("BTC/USDT", "0.66", "0.71", "+1.00R", "live equity", "<svg", "not checked yet",
                 "&lt;stop&gt;", "running"):
        assert text in r.text, text
    csrf = auth.csrf(token)
    assert client.post("/trading/pause", data={}, follow_redirects=False).status_code == 403
    client.post("/trading/pause", data={"csrf": csrf})
    assert "PAUSED" in client.get("/trading").text
    r = client.post("/trading/kill", data={"csrf": csrf, "confirm": "kill"}, follow_redirects=False)
    assert "type+KILL" in r.headers["location"] or "type%20KILL" in r.headers["location"]
    assert not json.load(open("data/trade/control.json"))["killed"]
    client.post("/trading/kill", data={"csrf": csrf, "confirm": "KILL"})
    assert json.load(open("data/trade/control.json"))["killed"]
    assert "KILL SWITCH ON" in client.get("/trading").text
    client.post("/trading/resume", data={"csrf": csrf})
    assert not json.load(open("data/trade/control.json"))["paused"]
    client.post("/trading/gate", data={"csrf": csrf})
    assert "does not pass yet" in client.get("/trading").text
    kinds = [json.loads(line)["kind"] for line in open("data/ops/events.jsonl")]
    assert kinds[:3] == ["risk_pause", "kill", "resume"]


def test_trading_page_status_tiles_and_grouped_events(web):
    from aitrader.ops import EventLog

    client, _auth, _runner, paths = web
    log = EventLog("data/ops/events.jsonl", service="executor-live")
    for _ in range(12):
        log.emit("error", "live executor: AuthenticationError: bybit requires apiKey", level="error")
    login(client)
    r = client.get("/trading")
    assert 'class="stat"' in r.text and "Bybit account" in r.text and "Testnet" not in r.text
    assert '<span class="v warn">no key</span>' in r.text and "Bybit real account (read only)" in r.text
    assert r.text.count("bybit requires apiKey") == 1 and "×12" in r.text  # repeats shown once
    assert "no strategy is paper trading yet" in r.text  # nothing paper trading yet
    assert '<a href="/trading" class=active aria-current=page>Trading</a>' in r.text


def test_paper_switch_and_live_card(web, monkeypatch):
    client, auth, runner, paths = web
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    csrf = auth.csrf(login(client))
    r = client.get("/trading")
    assert "Paper trading <b class=\"bad\">OFF</b>" in r.text and "Turn paper trading on" in r.text
    assert "Live trading <b class=\"bad\">OFF</b>" in r.text and "no switch for this on the website" in r.text
    assert "/trading/live" not in r.text  # live is never switched on from the website

    client.post("/trading/paper/off", data={"csrf": csrf})
    assert json.load(open("data/trade/control.json"))["paused"]
    assert client.post("/trading/paper/on", data={}, follow_redirects=False).status_code == 403
    r = client.post("/trading/paper/on", data={"csrf": csrf}, follow_redirects=False)
    assert "Paper+trading+is+on" in r.headers["location"] or "Paper%20trading%20is%20on" in r.headers["location"]
    assert not json.load(open("data/trade/control.json"))["paused"]
    steps, meta = submitted[0]
    assert meta == {"kind": "paper-start"}
    assert steps[:3] == [["learn", "add-library"], ["learn", "add-builtin"], ["learn", "paper-all"]]
    assert steps[3][0] == "download" and "--since" in steps[3]
    assert steps[4] == ["learn", "test", "--paper"]

    client.post("/trading/kill", data={"csrf": csrf, "confirm": "KILL"})
    r = client.post("/trading/paper/on", data={"csrf": csrf}, follow_redirects=False)
    assert "kill" in r.headers["location"].lower() and len(submitted) == 1
    assert json.load(open("data/trade/control.json"))["killed"]  # the kill switch is not cleared by it

    client.post("/trading/resume", data={"csrf": csrf})
    reg = Registry(paths.registry)
    reg.add(Entry(id="s1", kind="builtin", name="trend_pullback", status="paper"))
    reg.save()
    from aitrader.ops import EventLog

    EventLog(paths.events, service="paper").emit("heartbeat", "alive")
    assert "Paper trading <b class=\"good\">ON</b>" in client.get("/trading").text


def test_logout_survives_restart(tmp_path):
    path = str(tmp_path / "revoked.json")
    a = Auth("admin", PW, "fixed", revoked_path=path)
    tok = a.login("1.1.1.1", "admin", PW)
    keep = a.login("1.1.1.1", "admin", PW)
    a.revoke(tok)
    b = Auth("admin", PW, "fixed", revoked_path=path)  # the dashboard restarted
    assert b.verify(tok) is None and b.verify(keep) == "admin"


def test_uploads_need_a_login_before_the_body_is_read(web):
    client, *_ = web
    r = client.post("/books", files={"book": ("a.txt", b"x" * 1000)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert not os.path.exists("data/books")


def test_news_links_are_http_only():
    from aitrader.web.news_pages import _link

    assert _link("javascript:alert(1)") == "" and _link("data:text/html,x") == ""
    assert 'href="https://x.com/a"' in _link("https://x.com/a")


def test_trading_page_shows_the_real_bybit_balance_read_only(tmp_path, monkeypatch):
    import asyncio

    from aitrader.models import Balance
    from aitrader.web.account import RealAccount

    class Client:
        async def fetch_tickers(self, pairs):
            assert pairs == ["BTC/USDT", "SHIB/USDT"]
            return {"BTC/USDT": {"last": 60_000.0}, "SHIB/USDT": {"last": 0.00001}}

    class Adapter:
        made = []

        def __init__(self, key, secret):
            self.client, self.closed = Client(), False
            Adapter.made.append(self)

        async def balance(self):
            return Balance("bybit", free={"USDT": 120.5, "BTC": 0.001, "SHIB": 10.0}, used={"USDT": 4.5})

        async def close(self):
            self.closed = True

        async def place_order(self, *a, **k):  # pragma: no cover - must never be called
            raise AssertionError("the account card must never trade")

    env = {}
    acct = RealAccount(adapter_factory=Adapter, environ=env, clock=lambda: 0.0)
    assert asyncio.run(acct.snapshot()) == {"state": "no_key"}
    env.update(BYBIT_API_KEY="k" * 20, BYBIT_API_SECRET="s" * 20)
    snap = asyncio.run(acct.snapshot())
    assert snap["state"] == "ok" and snap["total_usdt"] == pytest.approx(185.0)
    assert [c["asset"] for c in snap["coins"]] == ["USDT", "BTC"]  # dust (SHIB) left out
    assert Adapter.made[-1].closed
    asyncio.run(acct.snapshot())
    assert len(Adapter.made) == 1  # cached for a minute

    class Refused(Exception):
        pass

    class AuthenticationError(Refused):
        pass

    async def refuse():
        raise AuthenticationError("https://api.bybit.com/...?sign=SECRET")

    bad = RealAccount(adapter_factory=lambda k, s: type("A", (Adapter,), {"balance": lambda self: refuse()})(k, s),
                      environ=env)
    snap = asyncio.run(bad.snapshot())
    assert snap["state"] == "error" and "refused the key" in snap["note"] and "SECRET" not in snap["note"]

    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), Auth("admin", PW, "k"),
                     JobRunner(str(tmp_path / "jobs")), Paths(), account=acct)
    client = TestClient(app)
    login(client)
    page = client.get("/trading").text
    assert "Bybit real account (read only)" in page and "185.00 USDT" in page and "<td>BTC</td>" in page
