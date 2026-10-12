"""User accounts: passwords, encrypted keys, key checks, the user pages, per-user trading."""

import asyncio
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from aitrader.accounts import AccountError, KeyRejected, KeyUnverified, UserStore, Vault, verify_keys
from aitrader.accounts.runner import CombinedControl, UsersRunner, user_control
from aitrader.config import LIVE_CONFIRM_PHRASE, Mode, Settings
from aitrader.ops import EventLog
from aitrader.trade import Control
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_trade import KEY, NOW, SIG, FakeAdapter, _paper, _pos

SECRET = "k" * 40
PW = "correct horse battery"
API_KEY, API_SECRET = "AbCdEfGhIjKlMnOpQrStUvWx1234", "s3cretS3cretS3cretS3cret9876"


def run(coro):
    return asyncio.run(coro)


# ---- store ------------------------------------------------------------------------------------


def test_accounts_passwords_and_status(tmp_path):
    st = UserStore(str(tmp_path / "a.db"))
    u = st.create("Ann@Example.com ", "a long enough password")
    assert u.email == "ann@example.com" and u.status == "pending" and u.subject == f"u:{u.id}"
    with pytest.raises(AccountError):
        st.create("ann@example.com", "another long password")
    with pytest.raises(AccountError):
        st.create("bob@example.com", "short")
    with pytest.raises(AccountError):
        st.create("not-an-email", "a long enough password")
    assert st.authenticate("ANN@example.com", "a long enough password").id == u.id
    assert st.authenticate("ann@example.com", "wrong password!!") is None
    assert st.authenticate("nobody@example.com", "a long enough password") is None
    raw = open(tmp_path / "a.db", "rb").read()
    assert b"a long enough password" not in raw  # only the scrypt hash is stored
    st.set_status(u.id, "disabled")
    assert st.authenticate("ann@example.com", "a long enough password") is None
    assert st.from_subject("admin") is None and st.from_subject(u.subject).id == u.id


def test_keys_are_encrypted_and_live_needs_everything(tmp_path):
    st = UserStore(str(tmp_path / "a.db"))
    v = Vault(SECRET)
    u = st.create("ann@example.com", "a long enough password", status="active")
    st.save_keys(v, u.id, "bybit", True, API_KEY, API_SECRET, {"ok": 1})
    assert st.load_keys(v, u.id, "bybit", True) == (API_KEY, API_SECRET)
    raw = open(tmp_path / "a.db", "rb").read()
    assert API_KEY.encode() not in raw and API_SECRET.encode() not in raw
    assert st.key_info(u.id)[0]["hint"] == "…" + API_KEY[-4:]
    with pytest.raises(Exception):
        st.load_keys(Vault("x" * 40), u.id, "bybit", True)  # another secret can't read them
    with pytest.raises(AccountError):
        Vault("short")
    with pytest.raises(AccountError, match="admin"):
        st.set_mode(u.id, "live", 100)
    st.allow_live(u.id, True)
    with pytest.raises(AccountError, match="real-account"):
        st.set_mode(u.id, "live", 100)
    st.save_keys(v, u.id, "bybit", False, API_KEY, API_SECRET, {})
    with pytest.raises(AccountError, match="budget"):
        st.set_mode(u.id, "live", 0)
    st.set_mode(u.id, "live", 250)
    assert st.get(u.id).mode == "live" and st.get(u.id).pilot_max_usdt == 250
    st.allow_live(u.id, False)
    assert st.get(u.id).mode == "testnet"


# ---- key checks -------------------------------------------------------------------------------


class KeyClient:
    def __init__(self, restrictions=None, fail=False):
        self.r, self.fail = restrictions or {}, fail
        self.markets = {}

    async def fetch_balance(self):
        if self.fail == "network":
            raise type("ExchangeNotAvailable", (Exception,), {})("GET https://...&signature=abc 503")
        if self.fail:
            raise type("AuthenticationError", (Exception,), {})("bad key")
        return {"free": {"USDT": 100.0}, "used": {}}

    async def private_get_v5_user_query_api(self):
        return {"retCode": 0, "result": self.r}

    async def close(self):
        pass


# what Bybit's /v5/user/query-api returns for a good trade-only key
GOOD = {"readOnly": 0, "ips": ["200.141.3.235"],
        "permissions": {"Spot": ["SpotTrade"], "Wallet": [], "ContractTrade": [], "Derivatives": [],
                        "Options": []}}


def _perm(**p):
    return {**GOOD, "permissions": {**GOOD["permissions"], **p}}


@pytest.mark.parametrize("raw,msg", [
    (_perm(Wallet=["AccountTransfer", "Withdraw"]), "WITHDRAW"),
    (_perm(Wallet=["AccountTransfer"]), "transfer"),
    (_perm(ContractTrade=["Order", "Position"]), "Derivatives"),
    (_perm(Spot=[]), "SPOT Trade"),
    ({**GOOD, "readOnly": 1}, "Read-Write"),
    ({**GOOD, "ips": ["*"]}, "IP"),
    ({**GOOD, "ips": []}, "IP"),
])
def test_real_keys_with_risky_permissions_are_refused(raw, msg):
    with pytest.raises(KeyRejected, match=msg):
        run(verify_keys("bybit", False, API_KEY, API_SECRET, Settings(), client=KeyClient(raw)))


def test_key_checks_pass_and_fail():
    perms = run(verify_keys("bybit", False, API_KEY, API_SECRET, Settings(), client=KeyClient(GOOD)))
    assert perms["wallet"] == [] and perms["ip_restricted"] and perms["spot"] == ["SpotTrade"]
    assert run(verify_keys("bybit", True, API_KEY, API_SECRET, Settings(mode=Mode.TESTNET),
                           client=KeyClient(fail=False)))
    with pytest.raises(KeyRejected, match="refused"):
        run(verify_keys("bybit", True, API_KEY, API_SECRET, Settings(), client=KeyClient(fail=True)))
    with pytest.raises(KeyUnverified, match="couldn't reach Bybit demo trading") as err:
        run(verify_keys("bybit", True, API_KEY, API_SECRET, Settings(), client=KeyClient(fail="network")))
    assert "signature" not in str(err.value)  # the request URL is never echoed
    with pytest.raises(KeyRejected, match="supported"):
        run(verify_keys("htx", False, API_KEY, API_SECRET, Settings(), client=KeyClient(GOOD)))
    with pytest.raises(KeyRejected):
        run(verify_keys("bybit", True, "short", API_SECRET, Settings(), client=KeyClient()))


# ---- web --------------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    users = UserStore(str(tmp_path / "accounts.db"))
    calls = []

    async def verifier(exchange, testnet, api_key, secret, settings):
        calls.append((exchange, testnet))
        if "withdraw" in api_key:
            raise KeyRejected("this key can WITHDRAW funds")
        return {"enableWithdrawals": False}

    def make(signup=True, settings=None):
        auth = Auth("admin", PW, "k")
        app = create_app(settings or Settings(), auth, JobRunner(str(tmp_path / "jobs")), Paths(),
                         users=users, keys_secret=SECRET, signup=signup, verifier=verifier)
        return TestClient(app), auth

    return make, users, calls


def _login(client, user, pw):
    r = client.post("/login", data={"user": user, "password": pw}, follow_redirects=False)
    return r, client.cookies.get(COOKIE)


def _csrf(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/me").text).group(1)


def test_signup_approval_keys_and_isolation(site):
    make, users, calls = site
    closed, _ = make(signup=False)
    assert "closed" in closed.get("/signup").text
    closed.post("/signup", data={"email": "x@example.com", "password": "p" * 12, "password2": "p" * 12, "agree": "1"})
    assert users.list() == []

    client, auth = make()
    assert "Create an account" in client.get("/login").text
    r = client.post("/signup", data={"email": "ann@example.com", "password": "a long enough password",
                                     "password2": "different password!", "agree": "1"}, follow_redirects=False)
    assert "differ" in r.headers["location"]
    r = client.post("/signup", data={"email": "ann@example.com", "password": "a long enough password",
                                     "password2": "a long enough password", "agree": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/login?msg=signed_up"
    r, token = _login(client, "ann@example.com", "a long enough password")
    assert r.headers["location"] == "/me"
    page = client.get("/me").text
    assert "waiting for the admin" in page and 'name="api_key"' not in page
    for path in ("/", "/trading", "/books", "/admin/users", "/strategies/x"):
        assert client.get(path, follow_redirects=False).headers["location"] == "/me", path
    csrf = _csrf(client)
    r = client.post("/me/keys", data={"csrf": csrf, "net": "testnet", "api_key": API_KEY, "secret": API_SECRET},
                    follow_redirects=False)
    assert "approved" in r.headers["location"] and not calls

    admin, admin_auth = make()
    _login(admin, "admin", PW)
    assert "ann@example.com" in admin.get("/admin/users").text
    acsrf = admin_auth.csrf(admin.cookies.get(COOKIE))
    uid = users.by_email("ann@example.com").id
    admin.post(f"/admin/users/{uid}/status", data={"csrf": acsrf, "status": "active"})
    assert users.get(uid).status == "active"
    assert admin.get("/me", follow_redirects=False).headers["location"] == "/"

    client2, _ = make()
    _login(client2, "ann@example.com", "a long enough password")
    csrf = _csrf(client2)
    assert client2.post("/me/keys", data={"net": "testnet", "api_key": API_KEY, "secret": API_SECRET},
                        follow_redirects=False).status_code == 403  # no CSRF token
    r = client2.post("/me/keys", data={"csrf": csrf, "net": "live", "api_key": "withdraw" + API_KEY,
                                       "secret": API_SECRET}, follow_redirects=False)
    assert "WITHDRAW" in r.headers["location"].replace("%20", " ") and not users.key_info(uid)
    client2.post("/me/keys", data={"csrf": csrf, "net": "testnet", "api_key": API_KEY, "secret": API_SECRET})
    page = client2.get("/me").text
    assert API_KEY not in page and API_SECRET not in page and API_KEY[-4:] in page
    r = client2.post("/me/live/on", data={"csrf": csrf, "budget": "100", "password": "a long enough password",
                                          "confirm": LIVE_CONFIRM_PHRASE}, follow_redirects=False)
    assert "not turned on" in r.headers["location"].replace("%20", " ") and users.get(uid).mode == "testnet"
    client2.post("/me/kill", data={"csrf": csrf, "confirm": "KILL"})
    assert user_control("data/users", uid).killed
    client2.post("/me/resume", data={"csrf": csrf})
    assert not user_control("data/users", uid).paused
    client2.post("/me/password", data={"csrf": csrf, "current": "a long enough password", "new": "a new long password"})
    assert users.authenticate("ann@example.com", "a new long password")

    admin.post(f"/admin/users/{uid}/status", data={"csrf": acsrf, "status": "disabled"})
    assert client2.get("/me", follow_redirects=False).headers["location"] == "/login"
    r, _ = _login(make()[0], "ann@example.com", "a new long password")
    assert "error=1" in r.headers["location"]


def test_live_switch(site, monkeypatch):
    make, users, _ = site
    import aitrader.web.user_pages as up

    gate = {"problems": ["the go/no-go gate hasn't been passed and approved yet"]}
    monkeypatch.setattr(up, "gate_problems", lambda gl: gate["problems"])
    monkeypatch.setattr("aitrader.accounts.live.gate_problems", lambda gl: gate["problems"])
    u = users.create("bob@example.com", "a long enough password", status="active")
    locked, _ = make()
    _login(locked, "bob@example.com", "a long enough password")
    page = locked.get("/me").text
    assert "Live trading <b>OFF</b>" in page and "admin hasn" in page and "live lock is closed" in page
    users.allow_live(u.id, True)
    users.save_keys(Vault(SECRET), u.id, "bybit", False, API_KEY, API_SECRET, {})
    unlocked = Settings(mode=Mode.LIVE, live_unlocked=True, live_confirm=LIVE_CONFIRM_PHRASE)
    client, _ = make(settings=unlocked)
    _login(client, "bob@example.com", "a long enough password")
    assert "gate hasn" in client.get("/me").text
    csrf = _csrf(client)
    good = {"csrf": csrf, "budget": "150", "password": "a long enough password", "confirm": LIVE_CONFIRM_PHRASE}

    def on(**change):
        r = client.post("/me/live/on", data={**good, **change}, follow_redirects=False)
        return r.headers["location"].replace("%20", " ").replace("+", " ")

    assert "gate" in on() and users.get(u.id).mode == "testnet"
    gate["problems"] = []
    assert 'name="budget"' in client.get("/me").text
    assert "password is wrong" in on(password="wrong password!!")
    assert "phrase" in on(confirm="yes")
    assert "budget" in on(budget="0")
    assert users.get(u.id).mode == "testnet"
    assert "ON" in on()
    assert users.get(u.id).mode == "live" and users.get(u.id).pilot_max_usdt == 150
    page = client.get("/me").text
    assert "LIVE (real money)" in page and "Turn live trading OFF now" in page
    client.post("/me/live/off", data={"csrf": csrf})
    assert users.get(u.id).mode == "testnet"
    from aitrader.accounts.live import live_stop_control

    assert live_stop_control("data/users", u.id).killed
    actions = [a["action"] for a in users.audit(u.id)]
    assert actions[:2] == ["live_off", "live_on"] and "live_on_refused" in actions
    kinds = [json.loads(line)["kind"] for line in open("data/ops/events.jsonl")]
    assert kinds.count("live_switch") == 2
    on()
    admin, admin_auth = make(settings=unlocked)
    _login(admin, "admin", PW)
    page = admin.get("/admin/users").text
    assert "Account log" in page and "live on" in page
    admin.post(f"/admin/users/{u.id}/live", data={"csrf": admin_auth.csrf(admin.cookies.get(COOKIE)), "allow": "off"})
    assert users.get(u.id).mode == "testnet" and not users.get(u.id).live_allowed
    assert users.audit(u.id)[0]["action"] == "live_not_allowed"


# ---- per-user trading -------------------------------------------------------------------------


def test_users_runner_trades_each_account_separately(tmp_path):
    st = UserStore(str(tmp_path / "a.db"))
    v = Vault(SECRET)
    paper = tmp_path / "paper.json"
    _paper(paper, {KEY: _pos(SIG)})
    adapters = {}

    def factory(exchange, settings, keys, testnet):
        assert testnet and settings.mode is Mode.TESTNET
        adapters[keys.api_key] = adapters.get(keys.api_key) or FakeAdapter(usdt=1000 if "A" in keys.api_key else 5000)
        return adapters[keys.api_key]

    a = st.create("a@example.com", "a long enough password", status="active")
    b = st.create("b@example.com", "a long enough password", status="active")
    c = st.create("c@example.com", "a long enough password")  # pending: has no keys anyway
    st.save_keys(v, a.id, "bybit", True, "A" * 20, "x" * 20, {})
    st.save_keys(v, b.id, "bybit", True, "B" * 20, "y" * 20, {})
    st.allow_live(b.id, True)  # live allowed, but the platform lock is closed -> still testnet
    platform = Control(str(tmp_path / "platform.json"))
    runner = UsersRunner(st, v, Settings(), str(paper), root=str(tmp_path / "users"),
                         platform_control=platform, adapter_factory=factory, clock=lambda: NOW)
    res = run(runner.tick())
    assert set(res) == {a.id, b.id} and c.id not in res
    assert all("error" not in r for r in res.values()), res
    sent_a = adapters["A" * 20].sent
    sent_b = adapters["B" * 20].sent
    assert len(sent_a) == 1 and len(sent_b) == 1
    assert sent_b[0].filled > sent_a[0].filled  # sized from each account's own balance
    assert os.path.exists(tmp_path / "users" / str(a.id) / "testnet_state.json")

    platform.kill(by="admin")  # the platform switch stops everyone
    run(runner.tick())
    assert all(len(x.sent) == 2 for x in adapters.values())
    platform.resume()
    st.set_status(a.id, "disabled")  # disabling sells and then stops that account
    assert user_control(str(tmp_path / "users"), a.id).killed is False
    run(runner.tick())
    assert user_control(str(tmp_path / "users"), a.id).killed
    run(runner.tick())
    run(runner.tick())
    assert a.id not in runner._ex


def test_combined_control():
    import tempfile

    d = tempfile.mkdtemp()
    own, plat = Control(os.path.join(d, "o.json")), Control(os.path.join(d, "p.json"))
    c = CombinedControl(own, plat)
    assert not c.paused
    plat.pause("platform pause")
    assert c.paused and not c.killed and c.state()["reason"].startswith("platform")
    plat.resume()
    c.kill(by="me")
    assert c.killed and own.killed and not plat.killed
    json.dumps(c.state())


def test_switching_live_off_sells_live_positions(tmp_path):
    from aitrader.accounts.live import live_stop_control, turn_off

    st = UserStore(str(tmp_path / "a.db"))
    v = Vault(SECRET)
    paper = tmp_path / "paper.json"
    _paper(paper, {KEY: _pos(SIG)})
    adapters = {}

    def factory(exchange, settings, keys, testnet):
        k = ("t" if testnet else "L") + keys.api_key
        adapters[k] = adapters.get(k) or FakeAdapter(usdt=2000)
        return adapters[k]

    u = st.create("a@example.com", "a long enough password", status="active")
    st.save_keys(v, u.id, "bybit", True, "T" * 20, "x" * 20, {})
    st.save_keys(v, u.id, "bybit", False, "R" * 20, "y" * 20, {})
    st.allow_live(u.id, True)
    st.set_mode(u.id, "live", 300)
    live = Settings(mode=Mode.LIVE, live_unlocked=True, live_confirm=LIVE_CONFIRM_PHRASE)
    root = str(tmp_path / "users")
    runner = UsersRunner(st, v, live, str(paper), root=root, platform_control=Control(str(tmp_path / "p.json")),
                         adapter_factory=factory, live_guard=lambda sid: [], live_monitor=lambda c: None,
                         clock=lambda: NOW)
    run(runner.tick())
    real = adapters["L" + "R" * 20]
    assert len(real.sent) == 1 and real.sent[0].filled * 100.1 <= 300 * 0.25 + 1e-6  # budget cap
    turn_off(st, root, u.id, actor="a@example.com", events=EventLog(str(tmp_path / "ev.jsonl")))
    assert st.get(u.id).mode == "testnet" and live_stop_control(root, u.id).killed
    run(runner.tick())  # the live executor keeps running only to sell
    assert len(real.sent) == 2 and real.sent[1].side.value == "sell"
    run(runner.tick())
    assert runner._ex[u.id][0][0] == "testnet"  # then the account is back on the testnet
    assert len(real.sent) == 2
