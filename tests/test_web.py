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
        json.dump({"open": {key: {"entry_id": "s1", "filled": True, "entry": 100.0, "mark_r": 0.4, "mark": 102.0, "ai_prob": 0.66,
                                  "units": 2.0, "signal": {"ts": 1, "entry": 100.0, "stop": 95.0}}},
                   "trades": [{"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_000_000_000,
                               "entry": 100, "exit": 105, "r": 1.0, "pnl_usdt": 50, "exit_reason": "target",
                               "ai_prob": 0.71},
                              {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_100_000_000,
                               "entry": 100, "exit": 98, "r": -0.4, "pnl_usdt": -20, "exit_reason": "<stop>"}]}, fh)
    with open("data/trade/live_state.json", "w") as fh:
        json.dump({"positions": {key: {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "amount": 0.5,
                                       "entry": 100.1, "stop": 95.0, "status": "open", "mark": 102.0,
                                       "cost": 50.05}},
                   "closed": [], "equity_curve": [[1, 10000], [2, 10050]]}, fh)
    token = login(client)
    r = client.get("/trading")
    assert r.status_code == 200
    for text in ("BTC/USDT", "0.66", "0.71", "+1.00R", "live equity", "<svg", "not checked yet",
                 "&lt;stop&gt;", "running"):
        assert text in r.text, text
    assert "Now USDT" in r.text and "+4.00" in r.text and "+0.95" in r.text  # 0.4R x 5 x 2; 102 x 0.5 - 50.05
    assert "<b>+4.95</b>" in r.text  # the total of the open positions
    assert '>102</td>' in r.text and "95 <span" in r.text and "6.86% away" in r.text  # (102 - 95) / 102
    assert 'data-reload-ms="60000"' in r.text and '<noscript><meta http-equiv="refresh" content="60">' in r.text
    js = client.get("/static/reload.js")  # a file, not inline: the page's security policy blocks inline scripts
    assert js.status_code == 200 and "activeElement" in js.text and "<script>" not in r.text
    assert 'autocapitalize="characters"' in r.text and "required" in r.text
    csrf = auth.csrf(token)
    assert client.post("/trading/pause", data={}, follow_redirects=False).status_code == 403
    client.post("/trading/pause", data={"csrf": csrf})
    assert "PAUSED" in client.get("/trading").text
    for wrong in ("", "kil", "stop"):
        r = client.post("/trading/kill", data={"csrf": csrf, "confirm": wrong}, follow_redirects=False)
        assert "KILL" in r.headers["location"] and r.headers["location"].endswith("&bad=1")
        assert not json.load(open("data/trade/control.json"))["killed"]
    assert '<p class="bad">Not pulled' in client.get(r.headers["location"]).text  # shown as an error
    client.post("/trading/kill", data={"csrf": csrf, "confirm": " Kill "})  # a phone capitalises the K
    assert json.load(open("data/trade/control.json"))["killed"]
    assert "KILL SWITCH ON" in client.get("/trading").text
    client.post("/trading/resume", data={"csrf": csrf})
    assert not json.load(open("data/trade/control.json"))["paused"]
    page = client.get("/trading").text
    assert page.count('action="/trading/close-all"') == 2  # in Controls and above the open positions
    assert client.post("/trading/close-all", data={}, follow_redirects=False).status_code == 403
    r = client.post("/trading/close-all", data={"csrf": csrf}, follow_redirects=False)
    assert "Closing" in r.headers["location"] and json.load(open("data/trade/close_all.json"))["by"] == "admin"
    assert not json.load(open("data/trade/control.json"))["paused"]  # paper trading keeps running
    # each paper position has its own Close button
    key = next(iter(json.load(open(paths.paper_state))["open"]))
    assert 'action="/trading/close-one"' in page and f'value="{key}"' in page
    assert client.post("/trading/close-one", data={"key": key}, follow_redirects=False).status_code == 403
    r = client.post("/trading/close-one", data={"csrf": csrf, "key": "nope"}, follow_redirects=False)
    assert r.headers["location"].endswith("&bad=1")
    r = client.post("/trading/close-one", data={"csrf": csrf, "key": key}, follow_redirects=False)
    assert "Closing%20the%20BTC/USDT" in r.headers["location"]
    assert json.load(open("data/trade/close_one.json"))[key]["by"] == "admin"
    assert "closing..." in client.get("/trading").text
    client.post("/trading/gate", data={"csrf": csrf})
    assert "does not pass yet" in client.get("/trading").text
    kinds = [json.loads(line)["kind"] for line in open("data/ops/events.jsonl")]
    assert kinds[:5] == ["risk_pause", "kill", "resume", "close_all", "close_one"]


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
    assert steps[:5] == [["learn", "add-library"], ["learn", "add-builtin"], ["learn", "add-timeframe", "30m"],
                         ["learn", "add-shorts"], ["learn", "paper-all"]]
    assert steps[5][0] == "download" and "--since" in steps[5]
    assert steps[6] == ["learn", "test", "--paper"] and steps[7] == ["learn", "keep-top"]

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


def test_keep_only_the_best_from_the_trading_page(web, monkeypatch):
    client, auth, runner, paths = web
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    csrf = auth.csrf(login(client))
    r = client.get("/trading")
    assert 'action="/trading/paper/keep"' in r.text and 'name="n" min="10" max="20" value="15"' in r.text
    assert client.post("/trading/paper/keep", data={"n": "12"}, follow_redirects=False).status_code == 403
    r = client.post("/trading/paper/keep", data={"csrf": csrf, "n": "50"}, follow_redirects=False)
    assert "Ranking" in r.headers["location"]
    assert submitted == [([["learn", "keep-top", "--n", "20"]], {"kind": "keep-top"})]  # clamped to 10-20
    r = client.post("/trading/paper/keep", data={"csrf": csrf, "n": "ten"}, follow_redirects=False)
    assert "Not+changed" in r.headers["location"] or "Not%20changed" in r.headers["location"]
    assert len(submitted) == 1


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
        async def fetch_tickers(self, *pairs):
            assert not pairs  # every ticker: a list fails whole when one coin has no USDT market
            return {"BTC/USDT": {"last": 60_000.0}, "SHIB/USDT": {"last": 0.00001}, "ETH/USDT": {"last": 1.0}}

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
    assert not Adapter.made[-1].closed  # one client kept for the key
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
    assert "updated 0 seconds ago" in page
    assert 'action="/trading/account/refresh"' in page


def test_real_balance_keeps_refreshing(tmp_path, monkeypatch):
    """Prices move the total each minute, a coin without a USDT market doesn't freeze the rest,
    a failed refresh keeps the last numbers with a note, and Refresh now asks again."""
    import asyncio

    from aitrader.models import Balance
    from aitrader.web.account import RealAccount

    price, fail, made = [60_000.0], [None], []

    class Client:
        async def fetch_tickers(self, *pairs):
            assert not pairs
            return {"BTC/USDT": {"last": price[0]}}  # no NEWCOIN/USDT market

    class Adapter:
        def __init__(self, key, secret):
            self.client = Client()
            made.append(self)

        async def balance(self):
            if fail[0]:
                raise fail[0]
            return Balance("bybit", free={"USDT": 100.0, "BTC": 0.01, "NEWCOIN": 5.0}, used={})

        async def close(self):
            pass

    clock = [0.0]
    acct = RealAccount(adapter_factory=Adapter, environ={"BYBIT_API_KEY": "k" * 20, "BYBIT_API_SECRET": "s" * 20},
                       clock=lambda: clock[0])

    async def run():
        snap = await acct.snapshot()
        assert snap["total_usdt"] == pytest.approx(700.0) and not snap["price_note"]
        assert {c["asset"]: c["usdt"] for c in snap["coins"]}["NEWCOIN"] is None
        price[0] = 70_000.0
        clock[0] = 61.0
        assert (await acct.snapshot())["total_usdt"] == pytest.approx(800.0)  # the minute passed: new prices
        assert len(made) == 1  # same client, markets loaded once

        class NetworkError(Exception):
            pass

        fail[0] = NetworkError("down")
        clock[0] = 122.0
        snap = await acct.snapshot()
        assert snap["state"] == "ok" and snap["total_usdt"] == pytest.approx(800.0)
        assert "couldn't reach Bybit" in snap["stale"]
        fail[0], price[0] = None, 50_000.0
        assert acct.refresh_now() is False  # asked a moment ago
        clock[0] = 140.0
        assert acct.refresh_now() is True
        snap = await acct.snapshot()
        assert snap["total_usdt"] == pytest.approx(600.0) and "stale" not in snap
        assert len(made) == 2  # a fresh client after the failure

    asyncio.run(run())

    from aitrader.web.app import _account_card
    card = _account_card({**snap_ok(), "stale": "couldn't reach Bybit just now"}, csrf="<input>", now=1_000.0 + 300)
    assert "Last refresh failed" in card and "5 minutes ago" in card and "Refresh now" in card

    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), Auth("admin", PW, "k"),
                     JobRunner(str(tmp_path / "jobs")), Paths(), account=acct)
    client = TestClient(app)
    csrf = Auth("admin", PW, "k").csrf(login(client))
    assert client.post("/trading/account/refresh", data={"csrf": "x"}).status_code == 403
    r = client.post("/trading/account/refresh", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "/trading?msg=" in r.headers["location"]


def snap_ok():
    return {"state": "ok", "coins": [{"asset": "USDT", "amount": 1.0, "free": 1.0, "usdt": 1.0}],
            "total_usdt": 1.0, "at": 1_000.0, "price_note": ""}


def test_ai_filter_train_turn_on_and_off_from_the_trading_page(web, monkeypatch):
    from test_ai import planted

    from aitrader.ai import ModelRegistry, train

    client, auth, runner, paths = web
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    csrf = auth.csrf(login(client))
    r = client.get("/trading")
    assert "AI trade filter" in r.text and "Train the AI filter" in r.text and "/trading/ai/on" not in r.text
    assert client.post("/trading/ai/train", data={}, follow_redirects=False).status_code == 403
    client.post("/trading/ai/train", data={"csrf": csrf})
    assert submitted == [([["ai", "retrain", "--all"]], {"kind": "ai-train"})]  # trains, never promotes

    reg = ModelRegistry(paths.models)
    model, rep = train(planted())
    good = reg.add(model, rep)
    _, bad_rep = train(planted(edge=False, seed=1))
    bad = reg.add(model, bad_rep)
    r = client.get("/trading")
    assert "rejected" in r.text and "/trading/ai/on" not in r.text  # the newest one failed its test
    r = client.post("/trading/ai/on", data={"csrf": csrf, "version": bad}, follow_redirects=False)
    assert "Not+turned+on" in r.headers["location"] or "Not%20turned%20on" in r.headers["location"]
    assert ModelRegistry(paths.models).active is None

    good2 = ModelRegistry(paths.models).add(model, rep)
    r = client.get("/trading")
    assert f'value="{good2}"' in r.text and f"Turn on {good2}" in r.text
    client.post("/trading/ai/on", data={"csrf": csrf, "version": good2})
    reg = ModelRegistry(paths.models)
    assert reg.active == good2 and reg.info(good2)["approved_by"] == "admin" and good != good2
    assert "Active model" in client.get("/").text
    assert "Turn the AI filter off" in client.get("/trading").text
    client.post("/trading/ai/off", data={"csrf": csrf})
    assert ModelRegistry(paths.models).active is None


def test_self_refreshing_pages_wait_while_someone_types():
    """The Trading and Monitor pages reload themselves, but not while a box (the kill switch's
    KILL) has the cursor or something typed in it, so the reload can't wipe it."""
    from aitrader.web.app import kill_confirmed

    assert kill_confirmed("KILL") and kill_confirmed("kill ") and not kill_confirmed("kil")


def test_open_positions_show_usdt_now():
    from aitrader.web.app import _live_usdt, _paper_usdt

    sig = {"entry": 100.0, "stop": 110.0}  # a paper short: risk 10 per unit
    assert _paper_usdt({"filled": True, "mark_r": -0.5, "units": 3, "signal": sig}) == -15.0
    assert _paper_usdt({"filled": False, "mark_r": 0.5, "units": 3, "signal": sig}) is None  # waiting
    assert _paper_usdt({"filled": True, "units": 3, "signal": sig}) is None  # no candle closed yet
    short = {"side": "short", "entry": 100.0, "mark": 98.0, "amount": 2, "entry_fee": 0.1}
    assert _live_usdt(short) == 3.9  # bought back cheaper, after the fee
    assert _live_usdt({"entry": 100.0, "amount": 2}) is None  # no price read yet


def test_open_positions_follow_the_live_price(tmp_path, monkeypatch):
    """The Trading page's Now USDT column updates every 1.5 s from /api/positions, which prices
    the open positions with the same cached Bybit feed as the Markets page."""
    from aitrader.web.app import live_position_usdt

    monkeypatch.chdir(tmp_path)

    class FakeLive:
        def __init__(self):
            self.asked = []

        async def tickers(self, symbols):
            self.asked.append(symbols)
            return {"source": "live", "tickers": [{"symbol": "BTC/USDT", "price": 103.0},
                                                  {"symbol": "ETH/USDT", "price": 48.0}]}

    fake = FakeLive()
    paths = Paths(paper_state="data/learn/paper_state.json")
    client = TestClient(create_app(Settings(), Auth("admin", PW, "k"),
                                   JobRunner(str(tmp_path / "jobs"), env_file="none"), paths, live=fake))
    assert client.get("/api/positions").status_code == 401
    login(client)
    assert client.get("/api/positions").json()["rows"] == {}  # nothing open: Bybit isn't asked
    assert fake.asked == []
    os.makedirs("data/learn", exist_ok=True)
    os.makedirs("data/trade", exist_ok=True)
    paper = {"open": {"a|bybit|BTC/USDT|1h": {"filled": True, "entry": 100.0, "units": 2.0, "mark_r": 0.1,
                                              "signal": {"entry": 100.0, "stop": 95.0, "side": "buy"}},
                      "b|bybit|ETH/USDT|4h": {"filled": True, "entry": 50.0, "units": 3.0,
                                              "signal": {"entry": 50.0, "stop": 52.0, "side": "sell"}},
                      "c|bybit|SOL/USDT|1h": {"filled": False, "signal": {"entry": 10.0, "stop": 9.0}}}}
    json.dump(paper, open(paths.paper_state, "w"))
    json.dump({"positions": {"k": {"key": "k", "entry_id": "a", "symbol": "BTC/USDT", "amount": 0.5, "entry": 100.0,
                                   "cost": 50.1, "stop": 95.0, "status": "open"}}}, open("data/trade/live_state.json", "w"))
    r = client.get("/api/positions").json()
    assert r["source"] == "live" and fake.asked[-1] == ["BTC/USDT", "ETH/USDT"]  # only filled ones
    assert r["rows"] == {"paper|a|bybit|BTC/USDT|1h": 6.0,  # (103 - 100) x 2
                         "paper|b|bybit|ETH/USDT|4h": 6.0,  # short: (50 - 48) x 3
                         "live|k": 1.4}  # 103 x 0.5 - 50.1
    assert r["total"] == 13.4
    assert r["prices"] == {"paper|a|bybit|BTC/USDT|1h": 103.0, "paper|b|bybit|ETH/USDT|4h": 48.0, "live|k": 103.0}
    page = client.get("/trading").text
    # the price now and the stop loss with how far away it is, for longs and shorts
    for cell in ("<th>Price now</th>", "<th>Stop loss</th>", 'data-px="paper|a|bybit|BTC/USDT|1h"',
                 'data-stop="paper|b|bybit|ETH/USDT|4h" data-level="52" data-short="1"'):
        assert cell in page, cell
    for cell in ('data-pos="paper|a|bybit|BTC/USDT|1h"', 'data-pos="live|k"', 'data-pos="total"',
                 '<script src="/static/positions.js" defer></script>'):
        assert cell in page, cell
    js = client.get("/static/positions.js")
    assert js.status_code == 200 and "/api/positions" in js.text
    assert live_position_usdt(paper, {}, {})["rows"] == {}  # no price: cells keep what they showed
    from aitrader.web.app import stop_distance
    assert stop_distance(100.0, 95.0, "buy") == 5.0 and stop_distance(48.0, 52.0, "sell") == 100 * 4 / 48
    assert stop_distance(94.0, 95.0, "buy") < 0 and stop_distance(None, 95.0, "buy") is None
