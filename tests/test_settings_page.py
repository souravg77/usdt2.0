"""The Settings page: API keys saved encrypted from the dashboard, first-time setup, health check."""

import os

import pytest
from fastapi.testclient import TestClient

from aitrader.accounts import KeyRejected
from aitrader.config import Settings
from aitrader.ops import secrets
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"
SECRET = "k" * 64
KEY = "AAAAAAAAAAAAAAAAAAAAA" + "x" * 40  # an X Bearer token


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for f in secrets.FIELDS:  # setenv first so teardown removes what the page sets
        monkeypatch.setenv(f.name, "")
        monkeypatch.delenv(f.name)
    monkeypatch.setenv("AITRADER_KEYS_SECRET", SECRET)
    calls = []
    down = {}  # checker name -> exception it raises (the check can't run)

    def x(token):
        calls.append(("x", token))
        if "x" in down:
            raise down["x"]
        if token == "bad":
            raise KeyRejected("X refused that token")

    def telegram(token, chat):
        calls.append(("telegram", token, chat))

    async def bybit(exchange, key, secret, settings):
        calls.append(("bybit", key))
        if "bybit" in down:
            raise down["bybit"]
        if key.startswith("c"):
            raise KeyRejected("this key can WITHDRAW funds")

    auth = Auth("admin", PW, "k")
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    paths = Paths(keys_store=str(tmp_path / "data/settings/keys.enc"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), auth, runner, paths, keys_secret=SECRET,
                     checkers={"x": x, "telegram": telegram, "bybit": bybit})
    client = TestClient(app)
    r = client.post("/login", data={"user": "admin", "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    token = client.cookies.get(COOKIE)
    client.down = down
    return client, auth.csrf(token), calls, paths, runner


def test_settings_needs_admin_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(Settings(), Auth("admin", PW, "k"),
                                   JobRunner(str(tmp_path / "j"), env_file="none")))
    for path in ("/settings",):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/settings/keys/x", data={"X_BEARER_TOKEN": KEY}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert not os.path.exists("data/settings/keys.enc")


def test_save_key_encrypted_and_masked(web):
    client, csrf, calls, paths, _ = web
    r = client.get("/settings")
    assert r.status_code == 200 and "Health check" in r.text and "X (Twitter)" in r.text
    assert "Anthropic" not in r.text  # the Anthropic API is no longer used: no key to save
    assert "unknown key group" in client.post("/settings/keys/anthropic", data={"csrf": csrf}).text
    r = client.post("/settings/keys/x", data={"csrf": "x" * 32, "X_BEARER_TOKEN": KEY})
    assert r.status_code == 403  # bad form token
    r = client.post("/settings/keys/x", data={"csrf": csrf, "X_BEARER_TOKEN": "bad"})
    assert "Not saved" in r.text and not os.path.exists(paths.keys_store)
    r = client.post("/settings/keys/x", data={"csrf": csrf, "X_BEARER_TOKEN": KEY})
    assert "checked and saved" in r.text
    assert KEY not in r.text and "•" * 12 + " xxxx" in r.text  # dots and the last 4 characters
    raw = open(paths.keys_store, "rb").read()
    assert KEY.encode() not in raw  # encrypted on disk
    assert oct(os.stat(paths.keys_store).st_mode & 0o777) == "0o600"
    assert os.environ["X_BEARER_TOKEN"] == KEY  # the dashboard uses it straight away
    env = {"AITRADER_KEYS_SECRET": SECRET}
    assert secrets.apply_to_env(paths.keys_store, env) == ["X_BEARER_TOKEN"] and env["X_BEARER_TOKEN"] == KEY
    r = client.post("/settings/keys/x/delete", data={"csrf": csrf})
    assert "Removed" in r.text and "X_BEARER_TOKEN" not in os.environ
    assert secrets.SecretStore(SECRET, paths.keys_store).load() == {}


def test_bybit_keys_are_checked_with_the_exchange(web):
    client, csrf, calls, paths, _ = web
    r = client.post("/settings/keys/bybit", data={"csrf": csrf, "BYBIT_API_KEY": "a" * 64})
    assert "needs both" in r.text
    r = client.post("/settings/keys/bybit", data={
        "csrf": csrf, "BYBIT_API_KEY": "c" * 64, "BYBIT_API_SECRET": "d" * 64})
    assert "WITHDRAW" in r.text  # a real key with withdrawal rights is refused
    assert secrets.SecretStore(SECRET, paths.keys_store).load() == {}
    r = client.post("/settings/keys/bybit", data={
        "csrf": csrf, "BYBIT_API_KEY": "a" * 64, "BYBIT_API_SECRET": "b" * 64})
    assert "checked and saved" in r.text and ("bybit", "a" * 64) in calls
    saved = secrets.SecretStore(SECRET, paths.keys_store).load()
    assert set(saved) == {"BYBIT_API_KEY", "BYBIT_API_SECRET"}
    assert "demo" not in client.get("/settings").text.lower()  # the demo option is gone
    assert client.post("/settings/keys/bybit_demo", data={"csrf": csrf}).text.count("unknown key group")


def test_telegram_sends_a_test_message(web):
    client, csrf, calls, *_ = web
    r = client.post("/settings/keys/telegram", data={"csrf": csrf, "TELEGRAM_BOT_TOKEN": "123:abc" + "z" * 30,
                                                     "TELEGRAM_CHAT_ID": "42"})
    assert "test message" in r.text and calls[-1][0] == "telegram"


def test_live_lock_can_never_be_stored(tmp_path):
    store = secrets.SecretStore(SECRET, str(tmp_path / "k.enc"))
    for name in ("AITRADER_LIVE_UNLOCKED", "AITRADER_MODE", "AITRADER_LIVE_CONFIRM"):
        with pytest.raises(ValueError):
            store.save({name: "true"})
    env = {"AITRADER_KEYS_SECRET": "wrong" * 10}
    store.save({"X_BEARER_TOKEN": KEY})
    assert secrets.apply_to_env(store.path, env) == []  # wrong secret: .env only, no crash


def test_setup_starts_one_job(web, monkeypatch):
    client, csrf, _calls, _paths, runner = web
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    r = client.post("/settings/setup", data={"csrf": csrf, "since": "2025-01-01; rm -rf /"})
    assert "Setup started" in r.text
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "setup"]
    assert len(jobs) == 1
    assert jobs[0]["steps"][0] == ["download", "--since", "2024-01-01", "--exchange", "bybit"]
    assert jobs[0]["steps"][1] == ["learn", "add-library", "--test"]
    r = client.post("/settings/setup", data={"csrf": csrf})
    assert "already running" in r.text
    runner.wait_idle(30)


def test_keys_are_saved_when_the_check_cannot_run(web):
    from aitrader.accounts import KeyUnverified

    client, csrf, calls, paths, _ = web
    client.down["x"] = KeyUnverified("X didn't confirm the token (HTTP 529)")
    client.down["bybit"] = KeyUnverified("couldn't reach Bybit to check the key (ExchangeNotAvailable)")

    r = client.post("/settings/keys/x", data={"csrf": csrf, "X_BEARER_TOKEN": KEY})
    assert "saved" in r.text and "HTTP 529" in r.text
    assert secrets.SecretStore(SECRET, paths.keys_store).load()["X_BEARER_TOKEN"] == KEY
    r = client.post("/settings/keys/bybit", data={
        "csrf": csrf, "BYBIT_API_KEY": "c" * 64, "BYBIT_API_SECRET": "d" * 64})
    assert "Not saved" in r.text  # a real-money key needs its permissions confirmed
    assert "BYBIT_API_KEY" not in secrets.SecretStore(SECRET, paths.keys_store).load()


def test_saved_keys_show_as_dots_and_can_be_tested_again(web):
    client, csrf, calls, paths, _ = web
    client.post("/settings/keys/x", data={"csrf": csrf, "X_BEARER_TOKEN": KEY})
    page = client.get("/settings").text
    assert KEY not in page and 'placeholder="••••••••••••' in page and "xxxx" in page
    assert "Last check: Working" in page and "Test saved key" in page

    # a bad replacement isn't saved and doesn't mark the saved key as broken
    client.post("/settings/keys/x", data={"csrf": csrf, "X_BEARER_TOKEN": "bad"})
    assert "Last check: Working" in client.get("/settings").text

    # test the saved key without typing it: it is checked with the stored value
    calls.clear()
    r = client.post("/settings/keys/x/test", data={"csrf": csrf})
    assert "X (Twitter): working." in r.text and calls == [("x", KEY)]
    client.down["x"] = KeyRejected("X says this token is invalid")
    r = client.post("/settings/keys/x/test", data={"csrf": csrf})
    assert "not working: X says this token is invalid" in r.text
    assert "Last check: Not working" in r.text
    assert secrets.SecretStore(SECRET, paths.keys_store).load()["X_BEARER_TOKEN"] == KEY  # kept

    r = client.post("/settings/keys/x/test", data={"csrf": "x" * 32})
    assert r.status_code == 403
    client.post("/settings/keys/x/delete", data={"csrf": csrf})
    page = client.get("/settings").text
    assert "Last check" not in page.split("X (Twitter)</h3>")[1].split("</div>")[0]


def test_check_and_save_with_empty_boxes_checks_the_saved_key(web):
    client, csrf, calls, paths, _ = web
    r = client.post("/settings/keys/bybit", data={"csrf": csrf})
    assert "paste the key first" in r.text  # nothing saved yet
    client.post("/settings/keys/bybit", data={
        "csrf": csrf, "BYBIT_API_KEY": "a" * 64, "BYBIT_API_SECRET": "b" * 64})
    calls.clear()
    r = client.post("/settings/keys/bybit", data={
        "csrf": csrf, "BYBIT_API_KEY": "", "BYBIT_API_SECRET": ""})
    assert "Bybit real account: working." in r.text and calls == [("bybit", "a" * 64)]
    assert "Last check: Working" in r.text
