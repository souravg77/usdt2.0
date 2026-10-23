"""Overnight book reading: books uploaded on the dashboard wait here, and the ``books`` service reads
them with the local book model (``aitrader.llm``) during the night hours, one at a time.

Each book runs ``aitrader learn read <book> --test``: the reader writes its strategies as checked
rules, then the backtest gate tests them and the ones that pass move to paper trading, as for any
strategy. Nothing here trades. A book can take hours on one CPU core, so it runs only at night
(``AITRADER_BOOK_HOURS``, local time, default ``0-6``: 00:00 to 06:00); a book started at night is
finished even if that runs past the end of the window, and the next one waits for the next night.

The queue is ``data/books/queue.json``; each book's output goes to a ``.log`` file next to it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

from ..clock import zone

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/books/queue.json"
DEFAULT_HOURS = "0-6"
CHECK_EVERY_S = 300
KEEP = 50  # finished books listed on the dashboard


def load(path: str = DEFAULT_PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        return [r for r in rows if isinstance(r, dict) and r.get("book")] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def save(rows: list[dict], path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    done = [r for r in rows if r.get("status") in ("done", "failed")]
    rows = [r for r in rows if r not in done or r in done[-KEEP:]]
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    os.replace(path + ".tmp", path)


def add(book: str, by: str = "", max_strategies: int = 12, test: bool = True, path: str = DEFAULT_PATH,
        now: float | None = None) -> dict:
    row = {"book": book, "by": by, "max": max(1, min(int(max_strategies), 30)), "test": bool(test),
           "added": now or time.time(), "status": "queued", "log": os.path.splitext(book)[0] + ".log"}
    rows = load(path)
    rows.append(row)
    save(rows, path)
    return row


def hours(env=None) -> tuple[int, int]:
    """The night window ``(start, end)`` in local hours, from ``AITRADER_BOOK_HOURS`` ("0-6")."""
    env = os.environ if env is None else env
    try:
        a, b = (int(x) for x in str(env.get("AITRADER_BOOK_HOURS") or DEFAULT_HOURS).split("-"))
        if 0 <= a <= 23 and 0 <= b <= 24 and a != b:
            return a, b
    except ValueError:
        pass
    return 0, 6


def is_night(now: float, window: tuple[int, int]) -> bool:
    """Whether ``now`` is inside the window; "22-6" runs across midnight."""
    h = datetime.fromtimestamp(now, zone()).hour
    a, b = window
    return a <= h < b if a < b else h >= a or h < b


def window_text(window: tuple[int, int]) -> str:
    return f"{window[0]:02d}:00 to {window[1] % 24:02d}:00"


def command(row: dict, registry: str) -> list[str]:
    cmd = [sys.executable, "-m", "aitrader.cli", "learn", "--registry", registry, "read", row["book"],
           "--max", str(row.get("max", 12))]
    return cmd + (["--test"] if row.get("test") else [])


def run_next(path: str = DEFAULT_PATH, registry: str = "data/learn/registry.json", run=subprocess.run,
             now: float | None = None) -> dict | None:
    """Read the oldest queued book; returns its row when done (None when nothing is queued)."""
    rows = load(path)
    row = next((r for r in rows if r.get("status") == "queued"), None)
    if row is None:
        return None
    row.update(status="reading", started=now or time.time())
    save(rows, path)
    try:
        with open(row["log"], "w", encoding="utf-8") as out:
            rc = run(command(row, registry), stdout=out, stderr=subprocess.STDOUT, check=False).returncode
        summary = ""
        with open(row["log"], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "new strategies from" in line:
                    summary = line.strip()[:300]
        row.update(status="done" if rc == 0 else "failed", rc=rc, summary=summary)
    except Exception as exc:  # the queue must keep going
        log.exception("reading %s failed", row["book"])
        row.update(status="failed", summary=f"{type(exc).__name__}: {exc}"[:300])
    row["finished"] = time.time()
    rows = load(path)  # books may have been added meanwhile
    for i, r in enumerate(rows):
        if r.get("book") == row["book"] and r.get("added") == row["added"]:
            rows[i] = row
    save(rows, path)
    return row


def serve(path: str = DEFAULT_PATH, registry: str = "data/learn/registry.json", every: float = CHECK_EVERY_S) -> None:
    """The ``books`` service: every few minutes, read the next queued book if it is night."""
    rows = load(path)
    for r in rows:  # a book cut off by a restart is read again from the start
        if r.get("status") == "reading":
            r["status"] = "queued"
    save(rows, path)
    while True:
        window = hours()
        if is_night(time.time(), window) and any(r.get("status") == "queued" for r in load(path)):
            row = run_next(path, registry)
            log.info("book %s: %s %s", row["book"], row["status"], row.get("summary", ""))
            continue  # the next one, if it is still night
        time.sleep(every)
