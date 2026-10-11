"""Shared trading switches: pause and kill (``data/trade/control.json``).

Any service (paper, executor) reads this every tick; the CLI, the dashboard and the Telegram
bot write it. ``paused`` stops new entries; ``kill`` also tells the executor to cancel every open
order and sell every position it opened. Both stay set until a person resumes.
"""

from __future__ import annotations

import json
import os
import time

DEFAULT_PATH = "data/trade/control.json"


class Control:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path

    def state(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return {"paused": False, "killed": False, "reason": "", **json.load(fh)}
        except (OSError, ValueError):
            return {"paused": False, "killed": False, "reason": "", "since": None, "by": ""}

    @property
    def paused(self) -> bool:
        s = self.state()
        return bool(s["paused"] or s["killed"])

    @property
    def killed(self) -> bool:
        return bool(self.state()["killed"])

    def pause(self, reason: str, by: str = "") -> dict:
        s = self.state()
        if s["paused"] or s["killed"]:
            return s  # keep the first reason
        return self._write({"paused": True, "killed": False, "reason": reason, "by": by})

    def kill(self, reason: str = "kill switch", by: str = "") -> dict:
        return self._write({"paused": True, "killed": True, "reason": reason, "by": by})

    def resume(self, by: str = "") -> dict:
        return self._write({"paused": False, "killed": False, "reason": "", "by": by})

    def mark_flattened(self) -> None:
        s = self.state()
        s["flattened_at"] = time.time()
        self._write(s, keep_since=True)

    def _write(self, s: dict, keep_since: bool = False) -> dict:
        if not keep_since:
            s["since"] = time.time()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1)
        os.replace(tmp, self.path)
        return s
