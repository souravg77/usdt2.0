"""Shared trading switches: pause and kill (``data/trade/control.json``).

Any service (paper, executor) reads this every tick; the CLI, the dashboard and the Telegram
bot write it. ``paused`` stops new entries; ``kill`` also tells the executor to cancel every open
order and sell every position it opened. ``halted`` is a pause the executor sets itself when
safety can't be checked (a position without its resting stop, the account not matching the
ledger, the price feed gone quiet); stops stay in place. All stay set until a person resumes.

A person can also ask for every paper position to be closed now (``request_close_all``, kept in
``close_all.json`` next to this file): the paper trader closes them at the last closed candle's
price on its next pass and keeps trading. A kill closes them too.

Paper trading stops only for a person: their pause or a kill (``stops_paper``). A safety halt or
the risk engine's loss-cap pause is about the live account, so paper keeps collecting results.
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
                return {"paused": False, "killed": False, "halted": False, "reason": "", **json.load(fh)}
        except (OSError, ValueError):
            return {"paused": False, "killed": False, "halted": False, "reason": "", "since": None, "by": ""}

    @property
    def paused(self) -> bool:
        s = self.state()
        return bool(s["paused"] or s["killed"])

    @property
    def killed(self) -> bool:
        return bool(self.state()["killed"])

    def pause(self, reason: str, by: str = "", auto: bool = False) -> dict:
        """``auto``: set by the risk engine, not a person (paper trading keeps going)."""
        s = self.state()
        if s["paused"] or s["killed"]:
            if not auto and not self._manual(s):  # a person's pause on top of an automatic one
                s["manual"] = True
                return self._write(s, keep_since=True)
            return s  # keep the first reason
        return self._write({"paused": True, "killed": False, "reason": reason, "by": by, "manual": not auto})

    @property
    def stops_paper(self) -> bool:
        """Paper trading opens nothing new: a kill, or a pause a person made."""
        s = self.state()
        return bool(s["killed"] or (s["paused"] and self._manual(s)))

    @staticmethod
    def _manual(s: dict) -> bool:
        if "manual" in s:
            return bool(s["manual"])
        # written before "manual" existed: automatic pauses say who made them
        by = str(s.get("by") or "")
        return not (s.get("halted") or by.startswith(("risk engine", "safety halt")))

    @property
    def halted(self) -> bool:
        return bool(self.state().get("halted"))

    def halt(self, reason: str, by: str = "") -> dict:
        """Hard pause: no new entries until a person resumes. A kill already set stays a kill."""
        s = self.state()
        if s["killed"]:
            return s
        if s.get("halted"):
            return s  # keep the first reason
        return self._write({"paused": True, "killed": False, "halted": True, "reason": reason, "by": by,
                            "manual": bool(s["paused"] and self._manual(s))})

    def kill(self, reason: str = "kill switch", by: str = "") -> dict:
        return self._write({"paused": True, "killed": True, "reason": reason, "by": by, "manual": True})

    def resume(self, by: str = "") -> dict:
        return self._write({"paused": False, "killed": False, "halted": False, "reason": "", "by": by})

    # ---- "close all open positions" (paper), a one-off request the paper trader carries out ----

    def _close_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "close_all.json")

    def request_close_all(self, by: str = "") -> dict:
        req = {"at": time.time(), "by": by}
        os.makedirs(os.path.dirname(self._close_path()), exist_ok=True)
        tmp = self._close_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(req, fh)
        os.replace(tmp, self._close_path())
        return req

    def close_all_requested(self) -> float | None:
        """When a person asked to close every paper position (epoch seconds), or None."""
        try:
            with open(self._close_path(), encoding="utf-8") as fh:
                return float(json.load(fh)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def clear_close_all(self, at: float) -> None:
        """Done with the request made at ``at`` (a newer request stays)."""
        if self.close_all_requested() == at:
            try:
                os.remove(self._close_path())
            except OSError:
                pass

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
