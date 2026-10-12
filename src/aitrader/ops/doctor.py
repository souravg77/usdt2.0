"""`aitrader doctor`: a health check of a running installation.

Each check returns PASS, WARN or FAIL with a line saying what to do. FAIL means something is
broken or unsafe; WARN means it works but should be looked at. The command exits 1 when any
check fails, so scripts (the installer, cron) can act on it.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import stat
import time

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
HEARTBEAT_STALE_MIN = 15


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _true(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "open", "on")


def run_checks(settings, root: str = ".", env_file: str = ".env", now: float | None = None) -> list[tuple]:
    from ..data.store import SCHEMA_VERSION
    from ..safety import live_gate_problems

    now = now or time.time()
    out: list[tuple] = []

    def add(level, name, text):
        out.append((level, name, text))

    p = lambda *parts: os.path.join(root, *parts)  # noqa: E731

    # ---- settings and secrets --------------------------------------------------------------------
    if os.path.exists(env_file):
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        if mode & 0o077:
            add(WARN, ".env permissions", f"{oct(mode)}: other users can read it; run chmod 600 .env")
        else:
            add(PASS, ".env permissions", "only the owner can read it")
    # (inside a container .env isn't mounted: Compose passes its values in, nothing to check)
    pw = _env("AITRADER_WEB_PASSWORD")
    add(PASS if len(pw) >= 12 else WARN, "dashboard password",
        "set" if len(pw) >= 12 else "AITRADER_WEB_PASSWORD is missing or under 12 characters: the dashboard won't start")
    add(PASS if len(_env("AITRADER_WEB_SECRET")) >= 32 else WARN, "dashboard session secret",
        "set" if len(_env("AITRADER_WEB_SECRET")) >= 32 else
        "AITRADER_WEB_SECRET is unset or short: everyone is logged out on each restart")
    site = _env("AITRADER_SITE")
    if site:
        ok = _true("AITRADER_WEB_SECURE_COOKIE")
        add(PASS if ok else WARN, "HTTPS settings",
            f"served at {site}" if ok else "with AITRADER_SITE set, also set AITRADER_WEB_SECURE_COOKIE=true")
    else:
        add(WARN, "HTTPS", "AITRADER_SITE is not set: the dashboard is only reachable by SSH tunnel")

    accounts_db = p("data", "accounts.db")
    n_keys = n_users = pending = 0
    if os.path.exists(accounts_db):
        try:
            con = sqlite3.connect(f"file:{accounts_db}?mode=ro", uri=True)
            n_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pending = con.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]
            n_keys = con.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
            con.close()
        except sqlite3.Error:
            add(WARN, "user accounts", "accounts.db could not be read")
    if len(_env("AITRADER_KEYS_SECRET")) >= 32:
        add(PASS, "key encryption secret", "set")
    elif n_keys:
        add(FAIL, "key encryption secret", f"{n_keys} stored exchange key(s) but AITRADER_KEYS_SECRET is missing: "
            "nobody's keys can be read; restore the original secret")
    elif _true("AITRADER_WEB_SIGNUP"):
        add(WARN, "key encryption secret", "sign-up is open but AITRADER_KEYS_SECRET is unset: users can't connect keys")
    if n_users:
        add(WARN if pending else PASS, "user accounts",
            f"{n_users} account(s), {pending} waiting for approval" if pending else f"{n_users} account(s)")
    if _true("AITRADER_WEB_SIGNUP") and not _true("AITRADER_WEB_SECURE_COOKIE"):
        add(WARN, "sign-up", "sign-up is open without HTTPS: passwords would cross the network unencrypted")

    # ---- money safety ---------------------------------------------------------------------------
    problems = live_gate_problems(settings)
    from ..trade.golive import approval_intact

    approval = _load(p("data", "golive", "approval.json"))
    if problems:
        add(PASS, "live lock", f"locked (mode {settings.mode.value}): no real-money orders")
    else:
        add(WARN, "live lock", "OPEN: real-money orders are possible")
        if approval and not approval_intact(approval):
            add(FAIL, "go-live approval", "approval.json was edited or not made by `golive approve`: approve again")
        elif not approval.get("strategies"):
            add(FAIL, "go-live approval", "the live lock is open but no strategy is approved (aitrader golive check)")
        if not _env("AITRADER_PILOT_MAX_USDT"):
            add(WARN, "pilot budget", "AITRADER_PILOT_MAX_USDT is unset: the executor-live service won't start")
    ctl = _load(p("data", "trade", "control.json"))
    if ctl.get("killed"):
        add(WARN, "kill switch", f"ON since {_ago(ctl.get('since'), now)} ({ctl.get('reason', '')}); aitrader trade resume")
    elif ctl.get("paused"):
        add(WARN, "trading paused", f"since {_ago(ctl.get('since'), now)}: {ctl.get('reason', '')}")

    # ---- data -----------------------------------------------------------------------------------
    db = settings.db_path if os.path.isabs(settings.db_path) else p(settings.db_path)
    if not os.path.exists(db):
        add(FAIL, "database", f"{db} not found: start the collector or run aitrader download")
    else:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            version = con.execute("PRAGMA user_version").fetchone()[0]
            add(PASS if version == SCHEMA_VERSION else FAIL, "database schema",
                f"v{version}" if version == SCHEMA_VERSION else f"v{version}, code expects v{SCHEMA_VERSION}: run aitrader migrate")
            row = con.execute("SELECT MAX(ts), COUNT(DISTINCT symbol) FROM candles").fetchone()
            con.close()
            if row[0] is None:
                add(WARN, "candles", "no candles yet: run aitrader download (see the guide)")
            else:
                age_h = (now * 1000 - row[0]) / 3_600_000
                add(PASS if age_h < 3 else WARN, "candles",
                    f"{row[1]} symbols, newest {age_h:.1f} h old" + ("" if age_h < 3 else ": is the collector running?"))
        except sqlite3.Error as exc:
            add(FAIL, "database", f"can't read {db}: {exc}")

    reg = _load(p("data", "learn", "registry.json"))
    n_paper = sum(1 for e in reg.get("strategies", []) if e.get("status") == "paper")
    add(PASS if n_paper else WARN, "strategies",
        f"{n_paper} in paper trading" if n_paper else "none in paper trading: press Turn paper trading on (Trading page), or run aitrader learn paper-all")

    # ---- services -------------------------------------------------------------------------------
    last: dict[str, float] = {}
    for path in (p("data", "ops", "events.jsonl"),):
        try:
            with open(path, encoding="utf-8") as fh:
                fh.seek(max(0, os.path.getsize(path) - 2_000_000))
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("kind") == "heartbeat":
                        last[ev.get("service") or "?"] = ev["ts"] / 1000
        except OSError:
            pass
    if not last:
        add(WARN, "services", "no heartbeats yet: is the paper service running?")
    for svc, ts in sorted(last.items()):
        mins = (now - ts) / 60
        add(PASS if mins <= HEARTBEAT_STALE_MIN else WARN, f"service {svc}",
            f"alive {mins:.0f} min ago" if mins <= HEARTBEAT_STALE_MIN else f"last heartbeat {mins:.0f} min ago: stopped?")

    # ---- disk and backups -----------------------------------------------------------------------
    data_dir = p("data")
    if os.path.isdir(data_dir):
        free_gb = shutil.disk_usage(data_dir).free / 2**30
        add(FAIL if free_gb < 0.5 else WARN if free_gb < 2 else PASS, "disk space", f"{free_gb:.1f} GB free")
        backups = sorted(glob.glob(p("data", "backups", "*")))
        if not backups:
            add(WARN, "backups", "none yet: run ./deploy/backup.sh once (the installer schedules it daily at 03:30)")
        else:
            age_d = (now - os.path.getmtime(backups[-1])) / 86400
            add(PASS if age_d < 8 else WARN, "backups", f"newest is {age_d:.1f} days old")
    return out


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _ago(ts, now: float) -> str:
    if not ts:
        return "?"
    h = (now - float(ts)) / 3600
    return f"{h:.1f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def report(checks: list[tuple]) -> tuple[str, int]:
    lines = [f"[{lvl}] {name}: {text}" for lvl, name, text in checks]
    fails = sum(1 for c in checks if c[0] == FAIL)
    warns = sum(1 for c in checks if c[0] == WARN)
    lines.append(f"{fails} problem(s), {warns} warning(s)")
    return "\n".join(lines), 1 if fails else 0
