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
KEY = "sk-ant-api03-" + "x" * 40


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for f in secrets.FIELDS:  # setenv first so teardown removes what the page sets
        monkeypatch.setenv(f.name, "")
        monkeypatch.delenv(f.name)
    monkeypatch.setenv("AITRADER_KEYS_SECRET", SECRET)
    calls = []

    def anthropic(key):
        calls.append(("anthropic", key))
        if key == "bad":
            raise KeyRejected("Anthropic refused that key")

    def telegram(token, chat):
        calls.append(("telegram", token, chat))

    async def binance(exchange, testnet, key, secret, settings):
        calls.append(("binance", testnet, key))
        if not testnet:
            raise KeyRejected("this key can WITHDRAW funds")

    auth = Auth("admin", PW, "k")
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    paths = Paths(keys_store=str(tmp_path / "data/settings/keys.enc"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), auth, runner, paths, keys_secret=SECRET,
                     checkers={"anthropic": anthropic, "telegram": telegram, "binance": binance})
    client = TestClient(app)
    r = client.post("/login", data={"user": "admin", "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    token = client.cookies.get(COOKIE)
    return client, auth.csrf(token), calls, paths, runner


def test_settings_needs_admin_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(Settings(), Auth("admin", PW, "k"),
                                   JobRunner(str(tmp_path / "j"), env_file="none")))
    for path in ("/settings",):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/settings/keys/anthropic", data={"ANTHROPIC_API_KEY": KEY}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert not os.path.exists("data/settings/keys.enc")


def test_save_anthropic_key_encrypted_and_masked(web):
    client, csrf, calls, paths, _ = web
    r = client.get("/settings")
    assert r.status_code == 200 and "Health check" in r.text and "Anthropic" in r.text
    r = client.post("/settings/keys/anthropic", data={"csrf": "x" * 32, "ANTHROPIC_API_KEY": KEY})
    assert r.status_code == 403  # bad form token
    r = client.post("/settings/keys/anthropic", data={"csrf": csrf, "ANTHROPIC_API_KEY": "bad"})
    assert "Not saved" in r.text and not os.path.exists(paths.keys_store)
    r = client.post("/settings/keys/anthropic", data={"csrf": csrf, "ANTHROPIC_API_KEY": KEY})
    assert "checked and saved" in r.text
    assert KEY not in r.text and "…xxxx" in r.text  # only the last 4 characters
    raw = open(paths.keys_store, "rb").read()
    assert KEY.encode() not in raw  # encrypted on disk
    assert oct(os.stat(paths.keys_store).st_mode & 0o777) == "0o600"
    assert os.environ["ANTHROPIC_API_KEY"] == KEY  # the dashboard uses it straight away
    env = {"AITRADER_KEYS_SECRET": SECRET}
    assert secrets.apply_to_env(paths.keys_store, env) == ["ANTHROPIC_API_KEY"] and env["ANTHROPIC_API_KEY"] == KEY
    r = client.post("/settings/keys/anthropic/delete", data={"csrf": csrf})
    assert "Removed" in r.text and "ANTHROPIC_API_KEY" not in os.environ
    assert secrets.SecretStore(SECRET, paths.keys_store).load() == {}


def test_binance_keys_are_checked_with_the_exchange(web):
    client, csrf, calls, paths, _ = web
    r = client.post("/settings/keys/binance_testnet",
                    data={"csrf": csrf, "BINANCE_TESTNET_API_KEY": "a" * 64})
    assert "needs both" in r.text
    r = client.post("/settings/keys/binance_testnet", data={
        "csrf": csrf, "BINANCE_TESTNET_API_KEY": "a" * 64, "BINANCE_TESTNET_API_SECRET": "b" * 64})
    assert "checked and saved" in r.text and ("binance", True, "a" * 64) in calls
    r = client.post("/settings/keys/binance", data={
        "csrf": csrf, "BINANCE_API_KEY": "c" * 64, "BINANCE_API_SECRET": "d" * 64})
    assert "WITHDRAW" in r.text  # a real key with withdrawal rights is refused
    saved = secrets.SecretStore(SECRET, paths.keys_store).load()
    assert set(saved) == {"BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"}


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
    store.save({"ANTHROPIC_API_KEY": KEY})
    assert secrets.apply_to_env(store.path, env) == []  # wrong secret: .env only, no crash


def test_setup_starts_one_job(web, monkeypatch):
    client, csrf, _calls, _paths, runner = web
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    r = client.post("/settings/setup", data={"csrf": csrf, "since": "2025-01-01; rm -rf /"})
    assert "Setup started" in r.text
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "setup"]
    assert len(jobs) == 1
    assert jobs[0]["steps"][0] == ["download", "--since", "2024-01-01", "--exchange", "binance"]
    assert jobs[0]["steps"][1] == ["learn", "add-library", "--test"]
    r = client.post("/settings/setup", data={"csrf": csrf})
    assert "already running" in r.text
    runner.wait_idle(30)
