"""The agent's strategy book: every strategy it has drafted, how it tested, and where it stands.

Statuses move one way through the pipeline:

    candidate -> rejected            (failed the backtest gate)
    candidate -> paper               (passed; now trades on paper with live candles)
    paper     -> retired             (paper results fell below the bar)

There is no "live" status. Moving a strategy to real money is a human decision made after the
Phase 6 go/no-go gate, through the existing live lock, never by this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

DEFAULT_PATH = "data/learn/registry.json"
STATUSES = ("candidate", "rejected", "paper", "retired")


@dataclass
class Entry:
    id: str
    kind: str  # "rule" (written by the agent) or "builtin" (hand-coded in strategies/)
    name: str
    status: str = "candidate"
    spec: dict | None = None
    source: str = ""
    added: float = field(default_factory=time.time)
    backtest: dict = field(default_factory=dict)
    paper: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def set_status(self, status: str, why: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        self.history.append({"at": time.time(), "from": self.status, "to": status, "why": why})
        self.status = status


class Registry:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self.entries: dict[str, Entry] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for raw in json.load(fh).get("strategies", []):
                    e = Entry(**raw)
                    self.entries[e.id] = e

    def add(self, entry: Entry) -> bool:
        """Add a strategy; returns False if the same rules are already registered."""
        if entry.id in self.entries:
            return False
        self.entries[entry.id] = entry
        return True

    def get(self, key: str) -> Entry:
        if key in self.entries:
            return self.entries[key]
        matches = [e for e in self.entries.values() if e.id.startswith(key) or e.name == key]
        if len(matches) != 1:
            raise KeyError(f"{key!r} matches {len(matches)} strategies")
        return matches[0]

    def with_status(self, *statuses: str) -> list[Entry]:
        return [e for e in self.entries.values() if e.status in statuses]

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "strategies": [e.__dict__ for e in self.entries.values()]},
                      fh, indent=1, default=str)
        os.replace(tmp, self.path)  # atomic, so the paper loop never reads half a file


def make_strategy(entry: Entry):
    from ..strategies import STRATEGIES
    from .rules import RuleStrategy

    if entry.kind == "builtin":
        return STRATEGIES[entry.name]()
    return RuleStrategy(entry.spec)
