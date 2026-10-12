"""Feed health on disk: the service running the Bybit price stream writes its ``status()`` every
few seconds, and the dashboard's Monitor page reads it (they are separate processes)."""

from __future__ import annotations

import asyncio
import json
import os
import time

DEFAULT_PATH = "data/ops/feed.json"


def save(status: dict, path: str = DEFAULT_PATH, now: float | None = None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({**status, "written": round(now if now is not None else time.time(), 1)}, fh)
    os.replace(tmp, path)


def load(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


async def report(stream, path: str = DEFAULT_PATH, every: float = 5.0) -> None:
    """Write ``stream.status()`` every ``every`` seconds until cancelled."""
    while True:
        try:
            save(stream.status(), path)
        except OSError:
            pass  # a full disk must never stop trading; the page shows the file as stale
        await asyncio.sleep(every)
