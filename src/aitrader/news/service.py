"""The ``news`` service: read the free feeds, let Claude search the web, save the brief.

The latest brief is ``data/news/latest.json`` (read by the dashboard and the trading filter);
every brief is also appended to ``data/news/history.jsonl``. Without an Anthropic key the feeds
are still saved and shown, but there is no AI brief and the filter does nothing.
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import feeds as feeds_mod
from .brief import poster, write_brief
from .sites import load_sites

log = logging.getLogger(__name__)

DEFAULT_DIR = "data/news"
HISTORY_KEEP = 500


def latest_path(news_dir: str = DEFAULT_DIR) -> str:
    return os.path.join(news_dir, "latest.json")


def load_latest(news_dir: str = DEFAULT_DIR) -> dict:
    try:
        with open(latest_path(news_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def x_reader(news_dir: str = DEFAULT_DIR):
    """The X reader when an X token is set, else None."""
    from .x import XReader

    return XReader(os.path.join(news_dir, "x.json")) if os.environ.get("X_BEARER_TOKEN") else None


def update(symbols: list[str], news_dir: str = DEFAULT_DIR, fetch=None, post=None,
           now_ms: int | None = None, events=None, max_searches: int | None = None, x="auto") -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    data = feeds_mod.gather(fetch or feeds_mod.fetch_url, now_ms)
    data["sites"] = load_sites(news_dir)
    x = x_reader(news_dir) if x == "auto" else x
    if x is not None:
        try:
            x.check_watch(symbols)
            x.search_pairs(symbols)
            data["x"] = x.for_brief()
        except Exception as exc:  # X is extra information: never let it stop the brief
            data["errors"].append(f"X: {_why(exc)}")
    out = {"generated": now_ms, "symbols": symbols, "feeds": data, "brief": None, "brief_error": ""}
    try:
        post = post or poster()
        kw = {"max_searches": max_searches} if max_searches else {}
        out["brief"] = write_brief(symbols, data, post, **kw)
    except Exception as exc:
        out["brief_error"] = _why(exc)
        log.warning("news brief failed: %s", out["brief_error"])
    previous = load_latest(news_dir)
    _save(out, news_dir)
    if events is not None:
        _emit(events, out, previous)
    return out


def _why(exc: Exception) -> str:
    text = str(exc)
    if hasattr(exc, "read"):  # urllib HTTPError: the API's own message says what is wrong
        try:
            text = json.loads(exc.read()).get("error", {}).get("message", text)
        except Exception:
            pass
    return f"{type(exc).__name__}: {text}"[:400]


def _save(out: dict, news_dir: str) -> None:
    os.makedirs(news_dir, exist_ok=True)
    path = latest_path(news_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    os.replace(tmp, path)
    if out["brief"]:
        hist = os.path.join(news_dir, "history.jsonl")
        row = {"generated": out["generated"], **{k: out["brief"][k] for k in ("market_risk", "market_risk_why")},
               "pairs": {p["symbol"]: p["risk"] for p in out["brief"]["pairs"]}}
        lines = []
        if os.path.exists(hist):
            with open(hist, encoding="utf-8") as fh:
                lines = fh.readlines()[-(HISTORY_KEEP - 1):]
        lines.append(json.dumps(row) + "\n")
        with open(hist + ".tmp", "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(hist + ".tmp", hist)


def _emit(events, out: dict, previous: dict) -> None:
    b = out["brief"]
    if not b:
        events.emit("news", f"news brief not written: {out['brief_error']}", level="warning")
        return
    before = (previous.get("brief") or {}).get("market_risk")
    risky = [p["symbol"] for p in b["pairs"] if p["risk"] == "high"]
    level = "warning" if b["market_risk"] == "high" or risky else "info"
    text = f"news: market risk {b['market_risk']}"
    if before and before != b["market_risk"]:
        text += f" (was {before})"
    text += f". {b['market_risk_why']}"
    if risky:
        text += f" High risk on {', '.join(risky)}: no new buys there."
    events.emit("news", text, level=level, searches=b.get("searches", 0))
