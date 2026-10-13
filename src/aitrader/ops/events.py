"""Append-only event log shared by the services (``data/ops/events.jsonl``).

One JSON object per line: ``{"ts": ms, "service": "paper", "kind": "trade_open", "level":
"info", "text": "...", "data": {...}}``. Writers append whole lines (atomic for lines this
small on Linux), readers tail the file by byte offset. Kinds the rest of the code relies on:

- ``trade_open`` / ``trade_close`` / ``signal_skipped``: trading activity
- ``error``: a caught exception in a service loop
- ``crash``: a service loop died (unhandled), counted by the Phase 6 stability check
- ``disconnect`` / ``reconnect``: exchange or data feed availability
- ``risk_pause`` / ``halt`` / ``resume`` / ``kill``: risk engine, safety halt and kill switch
- ``heartbeat``: written by ``Heartbeat`` every few minutes, so gaps show downtime
"""

from __future__ import annotations

import json
import os
import time

from .redact import redact, redact_data

DEFAULT_PATH = "data/ops/events.jsonl"
MAX_BYTES = 20 * 1024 * 1024  # rotate to events.jsonl.1 past this
ALERT_KINDS = {"trade_open", "trade_close", "error", "crash", "disconnect", "reconnect",
               "risk_pause", "halt", "resume", "kill", "daily_summary", "weekly_report", "gate", "live_switch"}


class EventLog:
    def __init__(self, path: str = DEFAULT_PATH, service: str = ""):
        self.path, self.service = path, service

    def emit(self, kind: str, text: str, level: str = "info", **data) -> dict:
        # an exchange error can carry a signed request: nothing secret reaches the log or alerts
        ev = {"ts": int(time.time() * 1000), "service": self.service, "kind": kind,
              "level": level, "text": redact(text), "data": redact_data(data)}
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        try:
            if os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, default=str) + "\n")
        return ev

    def read(self, since_ms: int = 0, kinds: set[str] | None = None, limit: int | None = None,
             include_rotated: bool = False) -> list[dict]:
        out = []
        paths = ([self.path + ".1"] if include_rotated else []) + [self.path]
        for p in paths:
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue  # a half-written line from a crash
                    if ev.get("ts", 0) >= since_ms and (kinds is None or ev.get("kind") in kinds):
                        out.append(ev)
        return out[-limit:] if limit else out

    def tail(self, offset: int) -> tuple[list[dict], int]:
        """Events written after byte ``offset``; returns (events, new offset). A rotated or
        truncated file starts again from 0."""
        if not os.path.exists(self.path):
            return [], 0
        size = os.path.getsize(self.path)
        if offset > size:
            offset = 0
        out = []
        with open(self.path, encoding="utf-8") as fh:
            fh.seek(offset)
            while True:
                line = fh.readline()
                if not line or not line.endswith("\n"):
                    break  # stop before a line still being written
                offset = fh.tell()
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out, offset


class Heartbeat:
    """Write a heartbeat at most every ``every_s`` seconds, from inside a service loop."""

    def __init__(self, log: EventLog, every_s: float = 300):
        self.log, self.every_s, self._last = log, every_s, 0.0

    def beat(self, **data) -> None:
        now = time.time()
        if now - self._last >= self.every_s:
            self._last = now
            self.log.emit("heartbeat", "alive", **data)
