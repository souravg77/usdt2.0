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
    key = "s1|binance|BTC/USDT|1h"
    with open(paths.paper_state, "w") as fh:
        json.dump({"open": {key: {"entry_id": "s1", "filled": True, "entry": 100.0, "mark_r": 0.4, "ai_prob": 0.66,
                                  "signal": {"ts": 1, "entry": 100.0, "stop": 95.0}}},
                   "trades": [{"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_000_000_000,
                               "entry": 100, "exit": 105, "r": 1.0, "pnl_usdt": 50, "exit_reason": "target",
                               "ai_prob": 0.71},
                              {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "exit_ts": 1_760_100_000_000,
                               "entry": 100, "exit": 98, "r": -0.4, "pnl_usdt": -20, "exit_reason": "<stop>"}]}, fh)
    with open("data/trade/testnet_state.json", "w") as fh:
        json.dump({"positions": {key: {"key": key, "entry_id": "s1", "symbol": "BTC/USDT", "amount": 0.5,
                                       "entry": 100.1, "stop": 95.0, "status": "open"}},
                   "closed": [], "equity_curve": [[1, 10000], [2, 10050]]}, fh)
    token = login(client)
    r = client.get("/trading")
    assert r.status_code == 200
    for text in ("BTC/USDT", "0.66", "0.71", "+1.00R", "testnet equity", "<svg", "not checked yet",
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
