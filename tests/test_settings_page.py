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
    down = {}  # checker name -> exception it raises (the check can't run)

    def anthropic(key, workspace=""):
        calls.append(("anthropic", key))
        if "anthropic" in down:
            raise down["anthropic"]
        if key == "bad":
            raise KeyRejected("Anthropic refused that key")

    def telegram(token, chat):
        calls.append(("telegram", token, chat))

    async def binance(exchange, testnet, key, secret, settings):
        calls.append(("binance", testnet, key))
        if "binance" in down:
            raise down["binance"]
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
    client.down = down
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
    assert KEY not in r.text and "•" * 12 + " xxxx" in r.text  # dots and the last 4 characters
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


class _Resp:
    def __init__(self, body=b'{"data": []}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _http_error(code, message):
    import io
    import json
    import urllib.error

    body = json.dumps({"type": "error", "error": {"type": "x", "message": message}}).encode()
    return urllib.error.HTTPError("https://api.anthropic.com/v1/models", code, "err", {}, io.BytesIO(body))


def test_anthropic_check_only_rejects_a_bad_key():
    from aitrader.accounts import KeyUnverified
    from aitrader.web.settings_pages import check_anthropic

    sent = []

    def ok(req, timeout):
        sent.append(req)
        return _Resp()

    check_anthropic(KEY, opener=ok)
    assert sent[0].get_header("X-api-key") == KEY
    with pytest.raises(KeyRejected, match="sk-ant-"):
        check_anthropic("my-claude-password", opener=ok)

    def refuse(code, msg):
        def op(req, timeout):
            raise _http_error(code, msg)
        return op

    with pytest.raises(KeyRejected, match="invalid") as e:
        check_anthropic(KEY, opener=refuse(401, "invalid x-api-key"))
    assert not isinstance(e.value, KeyUnverified)
    with pytest.raises(KeyUnverified, match="HTTP 403: Request not allowed"):
        check_anthropic(KEY, opener=refuse(403, "Request not allowed"))

    def offline(req, timeout):
        raise OSError("no route")

    with pytest.raises(KeyUnverified, match="OSError"):
        check_anthropic(KEY, opener=offline)

    # an org-level key: Anthropic needs the workspace header, which is sent once the ID is given
    msg = "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header"
    with pytest.raises(KeyRejected, match="Workspace ID") as e:
        check_anthropic(KEY, opener=refuse(400, msg))
    assert not isinstance(e.value, KeyUnverified)
    sent.clear()
    check_anthropic(KEY, "wrkspc_01abc", opener=ok)
    assert sent[0].get_header("Anthropic-workspace-id") == "wrkspc_01abc"


def test_book_reader_sends_the_workspace_id(monkeypatch):
    from aitrader.learn.extract import anthropic_headers

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert "anthropic-workspace-id" not in anthropic_headers(KEY)
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_01abc")
    assert anthropic_headers(KEY)["anthropic-workspace-id"] == "wrkspc_01abc"


def test_keys_are_saved_when_the_check_cannot_run(web):
    from aitrader.accounts import KeyUnverified

    client, csrf, calls, paths, _ = web
    client.down["anthropic"] = KeyUnverified("Anthropic didn't confirm the key (HTTP 529)")
    client.down["binance"] = KeyUnverified("couldn't reach Binance to check the key (ExchangeNotAvailable)")

    r = client.post("/settings/keys/anthropic", data={"csrf": csrf, "ANTHROPIC_API_KEY": KEY})
    assert "saved" in r.text and "HTTP 529" in r.text
    assert secrets.SecretStore(SECRET, paths.keys_store).load()["ANTHROPIC_API_KEY"] == KEY
    r = client.post("/settings/keys/binance_testnet", data={
        "csrf": csrf, "BINANCE_TESTNET_API_KEY": "a" * 64, "BINANCE_TESTNET_API_SECRET": "b" * 64})
    assert "saved" in r.text and "BINANCE_TESTNET_API_KEY" in secrets.SecretStore(SECRET, paths.keys_store).load()
    r = client.post("/settings/keys/binance", data={
        "csrf": csrf, "BINANCE_API_KEY": "c" * 64, "BINANCE_API_SECRET": "d" * 64})
    assert "Not saved" in r.text  # a real-money key needs its permissions confirmed
    assert "BINANCE_API_KEY" not in secrets.SecretStore(SECRET, paths.keys_store).load()


def test_saved_keys_show_as_dots_and_can_be_tested_again(web):
    client, csrf, calls, paths, _ = web
    client.post("/settings/keys/anthropic", data={"csrf": csrf, "ANTHROPIC_API_KEY": KEY})
    page = client.get("/settings").text
    assert KEY not in page and 'placeholder="••••••••••••' in page and "xxxx" in page
    assert "Last check: Working" in page and "Test saved key" in page

    # a bad replacement isn't saved and doesn't mark the saved key as broken
    client.post("/settings/keys/anthropic", data={"csrf": csrf, "ANTHROPIC_API_KEY": "bad"})
    assert "Last check: Working" in client.get("/settings").text

    # test the saved key without typing it: it is checked with the stored value
    calls.clear()
    r = client.post("/settings/keys/anthropic/test", data={"csrf": csrf})
    assert "Anthropic (Claude): working." in r.text and calls == [("anthropic", KEY)]
    client.down["anthropic"] = KeyRejected("Anthropic says this key is invalid")
    r = client.post("/settings/keys/anthropic/test", data={"csrf": csrf})
    assert "not working: Anthropic says this key is invalid" in r.text
    assert "Last check: Not working" in r.text
    assert secrets.SecretStore(SECRET, paths.keys_store).load()["ANTHROPIC_API_KEY"] == KEY  # kept

    r = client.post("/settings/keys/anthropic/test", data={"csrf": "x" * 32})
    assert r.status_code == 403
    client.post("/settings/keys/anthropic/delete", data={"csrf": csrf})
    page = client.get("/settings").text
    assert "Last check" not in page.split("Anthropic (Claude)</h3>")[1].split("</div>")[0]


def test_check_and_save_with_empty_boxes_checks_the_saved_key(web):
    client, csrf, calls, paths, _ = web
    r = client.post("/settings/keys/binance_testnet", data={"csrf": csrf})
    assert "paste the key first" in r.text  # nothing saved yet
    client.post("/settings/keys/binance_testnet", data={
        "csrf": csrf, "BINANCE_TESTNET_API_KEY": "a" * 64, "BINANCE_TESTNET_API_SECRET": "b" * 64})
    calls.clear()
    r = client.post("/settings/keys/binance_testnet", data={
        "csrf": csrf, "BINANCE_TESTNET_API_KEY": "", "BINANCE_TESTNET_API_SECRET": ""})
    assert "Binance testnet: working." in r.text and calls == [("binance", True, "a" * 64)]
    assert "Last check: Working" in r.text
