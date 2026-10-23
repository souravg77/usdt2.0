"""Hosting: `aitrader doctor`, the hardened docker-compose.yml and the deploy scripts."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from aitrader.config import Mode, Settings
from aitrader.data.store import CandleStore
from aitrader.models import Candle
from aitrader.ops.doctor import FAIL, PASS, WARN, report, run_checks
from aitrader.safety import LIVE_CONFIRM_PHRASE

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_760_000_000.0

GOOD_ENV = {
    "AITRADER_WEB_PASSWORD": "a-long-password-1",
    "AITRADER_WEB_SECRET": "s" * 64,
    "AITRADER_KEYS_SECRET": "k" * 64,
    "AITRADER_SITE": "https://192.168.1.50",
    "AITRADER_WEB_SECURE_COOKIE": "true",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("AITRADER_"):
            monkeypatch.delenv(k)
    for k, v in GOOD_ENV.items():
        monkeypatch.setenv(k, v)
    data = tmp_path / "data"
    store = CandleStore(str(data / "aitrader.db"))
    store.migrate()
    store.upsert([Candle("bybit", "BTC/USDT", "1h", int(NOW * 1000) - 3_600_000, 1, 1, 1, 1, 1)])
    store.close()
    (data / "learn").mkdir()
    (data / "learn" / "registry.json").write_text(json.dumps({"strategies": [{"status": "paper"}]}))
    (data / "ops").mkdir()
    (data / "ops" / "events.jsonl").write_text(
        json.dumps({"kind": "heartbeat", "service": "paper", "ts": int(NOW * 1000) - 60_000}) + "\n")
    (data / "backups" / "20261010").mkdir(parents=True)
    os.utime(data / "backups" / "20261010", (NOW - 3600, NOW - 3600))
    env = tmp_path / ".env"
    env.write_text("x=1\n")
    env.chmod(0o600)
    settings = Settings(db_path=str(data / "aitrader.db"), symbols=["BTC/USDT"])

    def check(**kw):
        return run_checks(kw.pop("settings", settings), root=str(tmp_path), env_file=str(env), now=NOW)

    return check, tmp_path, env


def _levels(checks):
    return {name: level for level, name, _ in checks}


def test_doctor_healthy_install_passes(world):
    check, *_ = world
    checks = check()
    text, code = report(checks)
    assert code == 0, text
    lv = _levels(checks)
    assert lv["live lock"] == PASS and lv["database schema"] == PASS and lv["candles"] == PASS
    assert lv["service paper"] == PASS and lv["strategies"] == PASS and lv["backups"] == PASS
    assert "pairs without prices" not in lv


def test_doctor_names_pairs_without_prices_and_ignores_retired_services(world):
    check, root, _env = world
    with open(root / "data" / "ops" / "events.jsonl", "a") as fh:  # demo trading's last beat, long ago
        fh.write(json.dumps({"kind": "heartbeat", "service": "executor-testnet", "ts": int(NOW * 1000) - 9 * 3_600_000}) + "\n")
    checks = check(settings=Settings(db_path=str(root / "data" / "aitrader.db"), symbols=["BTC/USDT", "XYZ/USDT"]))
    msgs = {name: (level, msg) for level, name, msg in checks}
    assert msgs["pairs without prices"][0] == WARN and "XYZ/USDT" in msgs["pairs without prices"][1]
    assert "BTC/USDT" not in msgs["pairs without prices"][1]
    assert "service executor-testnet" not in msgs and msgs["service paper"][0] == PASS


def test_doctor_flags_unsafe_and_broken_setups(world, monkeypatch):
    check, root, env = world
    env.chmod(0o644)
    monkeypatch.delenv("AITRADER_WEB_SECURE_COOKIE")
    lv = _levels(check())
    assert lv[".env permissions"] == WARN and lv["HTTPS settings"] == WARN

    # live lock open but nothing approved: a hard failure
    live = Settings(mode=Mode.LIVE, live_unlocked=True, live_confirm=LIVE_CONFIRM_PHRASE,
                    db_path=str(root / "data" / "aitrader.db"))
    checks = check(settings=live)
    lv = _levels(checks)
    assert lv["live lock"] == WARN and lv["go-live approval"] == FAIL and report(checks)[1] == 1

    # stored user keys but the secret that encrypts them is gone
    from aitrader.accounts.store import UserStore, Vault
    users = UserStore(str(root / "data" / "accounts.db"))
    u = users.create("a@example.com", "a-long-password-1", status="active")
    users.save_keys(Vault("k" * 64), u.id, "bybit", True, "KEY", "SECRET", {})
    users.close()
    monkeypatch.delenv("AITRADER_KEYS_SECRET")
    assert _levels(check())["key encryption secret"] == FAIL

    # stale services and a missing database
    (root / "data" / "ops" / "events.jsonl").write_text(
        json.dumps({"kind": "heartbeat", "service": "paper", "ts": int(NOW * 1000) - 3_600_000}) + "\n")
    os.remove(root / "data" / "aitrader.db")
    lv = _levels(check())
    assert lv["service paper"] == WARN and lv["database"] == FAIL


def _compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_is_hardened():
    services = _compose()["services"]
    for name, svc in services.items():
        assert svc.get("read_only") is True, name
        assert svc.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        assert svc.get("mem_limit"), name
    builders = [name for name, svc in services.items() if "build" in svc]
    assert builders == ["collector", "claude"]  # one build per image tag, or parallel builds collide
    assert all(svc["image"] == "aitrader:latest" for n, svc in services.items() if n not in ("caddy", "claude"))
    # Claude Code's own container: never the keys (no env_file, .env hidden), the data read-only
    claude = services["claude"]
    assert claude["profiles"] == ["claude"] and "env_file" not in claude
    assert "/dev/null:/work/.env:ro" in claude["volumes"] and "./data:/work/data:ro" in claude["volumes"]
    assert not any("docker.sock" in v for v in claude["volumes"])
    assert services["web"]["ports"] == ["127.0.0.1:8080:8080"]  # only Caddy faces the network
    assert services["web"]["environment"]["AITRADER_WEB_TRUST_PROXY"] == "true"
    assert services["web"]["environment"]["AITRADER_DB_PATH"]
    assert "env_file" not in services["caddy"]  # no app secrets in the proxy
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert services["executor-live"]["restart"] == "no" and services["executor-live"]["profiles"] == ["live"]
    for name, svc in services.items():
        env = svc.get("environment") or {}
        assert str(env.get("AITRADER_LIVE_UNLOCKED", "false")).lower() != "true", name
        if name not in ("caddy", "claude"):
            assert env.get("AITRADER_DB_PATH") == "/app/data/aitrader.db", name


@pytest.mark.parametrize("script", ["install.sh", "update.sh", "backup.sh"])
def test_deploy_scripts_parse_and_are_executable(script):
    path = ROOT / "deploy" / script
    assert os.access(path, os.X_OK)
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text()
    assert "AITRADER_LIVE_UNLOCKED=true" not in text.replace("grep -q '^AITRADER_LIVE_UNLOCKED=true'", "")


def test_backup_includes_accounts_and_user_state(tmp_path, monkeypatch):
    from aitrader import cli
    from aitrader.accounts import UserStore

    monkeypatch.chdir(tmp_path)
    os.makedirs("data/users/1")
    with open("data/users/1/control.json", "w") as fh:
        fh.write("{}")
    UserStore("data/accounts.db").close()
    s = Settings(db_path="data/aitrader.db")
    CandleStore(s.db_path).close()

    class Args:
        dest = "data/backups/x"

    assert cli.cmd_backup(s, Args()) == 0
    got = set(os.listdir("data/backups/x"))
    assert {"aitrader.db", "accounts.db", "data__users__1__control.json"} <= got


def test_doctor_reads_the_newest_candles_without_scanning_the_table(tmp_path):
    import sqlite3

    from aitrader.ops.doctor import _newest_candles

    con = sqlite3.connect(str(tmp_path / "c.db"))
    con.execute("CREATE TABLE candles (exchange TEXT, symbol TEXT, timeframe TEXT, ts INTEGER, "
                "PRIMARY KEY (exchange, symbol, timeframe, ts)) WITHOUT ROWID")
    con.executemany("INSERT INTO candles VALUES (?,?,?,?)", [
        ("binance", "BTC/USDT", "1h", 5), ("bybit", "BTC/USDT", "1d", 9), ("bybit", "BTC/USDT", "1h", 7),
        ("bybit", "ETH/USDT", "5m", 3), ("bybit", "ETH/USDT", "5m", 4)])
    assert _newest_candles(con) == {"BTC/USDT": 9, "ETH/USDT": 4}
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT MIN(symbol) FROM candles WHERE exchange = 'bybit' AND symbol > ''"))
    assert "SCAN" not in plan  # an index lookup, not a pass over every candle
    assert _newest_candles(sqlite3.connect(":memory:").execute(
        "CREATE TABLE candles (exchange, symbol, timeframe, ts)").connection) == {}


def test_cleanup_frees_space_but_never_touches_the_data():
    text = open("deploy/cleanup.sh", encoding="utf-8").read()
    assert "image prune -f" in text and "builder prune -f" in text
    assert "sudo -n" in text and "sudo apt" not in text  # never waits for a password
    for keep in ("aitrader.db", "backups", ".env", "volume prune", "system prune -a", "rm -rf"):
        assert keep not in text.split("set -uo pipefail")[1]
    install = open("deploy/install.sh", encoding="utf-8").read()
    assert "./deploy/cleanup.sh" in install and "0 5 * * 0" in install
