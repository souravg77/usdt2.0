"""User controls from the NEXUS UI plan, kept in one small file (``data/settings/controls.json``).

The Controls page (``web.controls_pages``) is one place for the switches a person sets. Some write
settings the engines already read, so there is one source of truth:

- **risk level** (Conservative / Balanced / Aggressive) fills in each paper engine's risk per trade,
  daily loss pause, weekly close and open-risk cap (``learn.guards``) and the paper futures leverage
  (``learn.wallet``). Each of those can still be changed on its own afterwards: the level then reads
  "custom".
- **max daily loss** and **trading timeframes** are the guards' ``day_pause_pct`` and ``timeframes``.

Three things are kept here, because nothing held them before:

- **blocked pairs**: never opened by either paper engine, from any list (fixed, ranked, top gainers,
  squeeze). Open positions on them still run to their exits. Read by ``market.pairs.configured_symbols``
  on every tick, so it takes effect within a minute without a restart.
- **automatic strategy generation and evolution**: ``aitrader learn auto --run`` (the ``factory``
  service) runs the Strategy Factory and/or Evolution every ``every_hours`` hours when switched on,
  then backtests what it made. It never touches live trading.
- who changed what, and when.

The live lock is not a control here: live orders need ``AITRADER_LIVE_UNLOCKED`` and the
confirmation phrase in ``.env`` and your ``golive approve`` at a terminal. The page shows that state
and cannot change it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/settings/controls.json"
TIMEFRAMES = ("15m", "30m", "1h", "4h")  # what the factory can build on (learn.factory.TIMEFRAMES)
AUTO_DEFAULTS = {"generate": False, "evolve": False, "n": 20, "timeframes": list(TIMEFRAMES),
                 "shorts": True, "every_hours": 24.0, "last_run": 0.0, "last_result": ""}
AUTO_N = (1, 100)
AUTO_HOURS = (1.0, 168.0)

# Paper engine settings per level (learn.guards keys) and the paper futures leverage. Risk per trade
# stays at or below the 1% hard cap the live risk engine enforces (trade.risk.HARD_MAX_RISK).
RISK_LEVELS = {
    "conservative": {"risk_pct": 0.25, "day_pause_pct": 2.0, "week_close_pct": 5.0, "open_risk_pct": 2.0,
                     "leverage": 1.0},
    "balanced": {"risk_pct": 0.5, "day_pause_pct": 3.0, "week_close_pct": 6.0, "open_risk_pct": 4.0,
                 "leverage": 2.0},
    "aggressive": {"risk_pct": 1.0, "day_pause_pct": 5.0, "week_close_pct": 10.0, "open_risk_pct": 8.0,
                   "leverage": 3.0},
}
GUARD_KEYS = ("risk_pct", "day_pause_pct", "week_close_pct", "open_risk_pct")


def path_from_env(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("AITRADER_CONTROLS_FILE") or DEFAULT_PATH


def _clean_auto(a: dict) -> dict:
    out = dict(AUTO_DEFAULTS)
    for k in ("generate", "evolve", "shorts"):
        if k in a:
            out[k] = bool(a[k]) and a[k] not in ("0", "", "false")
    try:
        out["n"] = int(min(max(int(a.get("n", out["n"])), AUTO_N[0]), AUTO_N[1]))
    except (TypeError, ValueError):
        pass
    try:
        out["every_hours"] = float(min(max(float(a.get("every_hours", out["every_hours"])), AUTO_HOURS[0]),
                                       AUTO_HOURS[1]))
    except (TypeError, ValueError):
        pass
    tfs = a.get("timeframes", out["timeframes"])
    if isinstance(tfs, str):
        tfs = tfs.split(",")
    out["timeframes"] = [t for t in TIMEFRAMES if isinstance(tfs, (list, tuple)) and t in tfs] or list(TIMEFRAMES)
    for k in ("last_run",):
        try:
            out[k] = float(a.get(k, 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0.0
    out["last_result"] = str(a.get("last_result", ""))[:500]
    return out


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    blocked = data.get("blocked_pairs") or []
    return {"auto": _clean_auto(data.get("auto") if isinstance(data.get("auto"), dict) else {}),
            "blocked_pairs": sorted({str(p).strip().upper() for p in blocked if str(p).strip()})
            if isinstance(blocked, list) else [],
            "by": str(data.get("by", "")), "at": data.get("at", 0)}


def save(path: str, changes: dict, by: str = "") -> dict:
    cur = load(path)
    if "auto" in changes:
        cur["auto"] = _clean_auto({**cur["auto"], **changes["auto"]})
    if "blocked_pairs" in changes:
        cur["blocked_pairs"] = sorted({normalize_pair(p) for p in changes["blocked_pairs"] if normalize_pair(p)})
    cur.update(by=by, at=int(time.time() * 1000))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1)
    os.replace(tmp, path)
    return load(path)


def normalize_pair(p: str) -> str:
    """``btc``, ``BTCUSDT``, ``btc/usdt`` -> ``BTC/USDT``; '' for nothing usable."""
    p = str(p).strip().upper().replace(" ", "")
    if not p:
        return ""
    p = p.split(":")[0]
    if "/" not in p:
        p = p[:-4] + "/USDT" if p.endswith("USDT") and len(p) > 4 else p + "/USDT"
    base, _, quote = p.partition("/")
    return f"{base}/{quote}" if base.isalnum() and quote.isalnum() else ""


def blocked_pairs(env=None) -> set[str]:
    """The blocked pairs, read fresh each call (cheap: a small file)."""
    return set(load(path_from_env(env))["blocked_pairs"])


def without_blocked(symbols: list[str], env=None, keep=()) -> list[str]:
    """``symbols`` minus the blocked pairs (spot or perpetual names), except those in ``keep``."""
    blocked = blocked_pairs(env)
    if not blocked:
        return symbols
    keep = set(keep or ())
    return [s for s in symbols if s in keep or s.split(":")[0].upper() not in blocked]


# ---- risk level ---------------------------------------------------------------------------------


def risk_level(guards_now: dict, leverage: float | None) -> str:
    """Which preset the engines' current settings match, or "custom"."""
    for name, lv in RISK_LEVELS.items():
        if all(abs(float(guards_now[m].get(k, -1)) - lv[k]) < 1e-9 for m in guards_now for k in GUARD_KEYS) \
                and leverage is not None and abs(leverage - lv["leverage"]) < 1e-9:
            return name
    return "custom"


def apply_risk_level(level: str, state_path: str, by: str = "") -> dict:
    """Write a preset into both paper engines' guards and the paper futures leverage."""
    from ..learn import guards, wallet

    if level not in RISK_LEVELS:
        raise ValueError(f"risk level must be one of {', '.join(RISK_LEVELS)}")
    lv = RISK_LEVELS[level]
    gpath = guards.path_for(state_path)
    for m in wallet.MARKETS:
        guards.save(gpath, m, {k: lv[k] for k in GUARD_KEYS}, by=by)
    wallet.set_leverage(wallet.market_path(state_path), lv["leverage"], by=by)
    return lv


# ---- automatic generation -----------------------------------------------------------------------


def due(auto: dict, now: float) -> bool:
    return (auto["generate"] or auto["evolve"]) and now - auto["last_run"] >= auto["every_hours"] * 3600


def steps(auto: dict) -> list[list[str]]:
    """The ``aitrader`` commands one automatic run makes."""
    out = []
    tfs = ",".join(auto["timeframes"])
    shorts = ["--shorts"] if auto["shorts"] else []
    if auto["generate"]:
        out.append(["learn", "factory", "--n", str(auto["n"]), "--timeframes", tfs, *shorts])
    if auto["evolve"]:
        out.append(["learn", "evolve", "--n", str(auto["n"]), "--timeframes", tfs, *shorts])
    if out:
        out.append(["learn", "test"])
        if auto["shorts"]:
            out.append(["learn", "test", "--market", "futures"])
    return out


def run_auto(path: str, env_file: str = ".env", now=time.time, runner=None, timeout: float = 6 * 3600) -> str:
    """One automatic run if it is due; returns what happened ('' when nothing was due). Each step
    runs as its own ``aitrader`` process, like a dashboard job."""
    c = load(path)
    auto = c["auto"]
    t = now()
    if not due(auto, t):
        return ""
    runner = runner or (lambda cmd: subprocess.run(cmd, timeout=timeout, check=False).returncode)
    save(path, {"auto": {"last_run": t, "last_result": "running"}}, by=c["by"])
    done, result = [], "ok"
    for step in steps(auto):
        cmd = [sys.executable, "-m", "aitrader.cli", "--env-file", env_file, *step]
        try:
            rc = runner(cmd)
        except subprocess.TimeoutExpired:
            rc = "timeout"
        done.append(" ".join(step[:2]))
        if rc != 0:
            result = f"`{' '.join(step[:2])}` ended with {rc}"
            break
    text = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}: {', '.join(done)}: {result}"
    save(path, {"auto": {"last_result": text}}, by=c["by"])
    log.info("automatic strategy run: %s", text)
    return text


def run_forever(path: str, env_file: str = ".env", check_every: float = 300.0) -> None:
    while True:
        try:
            run_auto(path, env_file)
        except Exception:  # keep the service alive; the next check tries again
            log.exception("automatic strategy run failed")
        time.sleep(check_every)
