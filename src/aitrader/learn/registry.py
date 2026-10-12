"""The agent's strategy book: every strategy it has drafted, how it tested, and where it stands.

Each strategy sits on the promotion ladder (docs/promotion-ladder.md):

    RESEARCH -> BACKTESTED -> OUT-OF-SAMPLE VALIDATED -> PAPER -> SHADOW MONITORED
             -> LIVE-ELIGIBLE -> LIVE ENABLED WITH EXPLICIT APPROVAL
    off the ladder: REJECTED, UNDER REVIEW, RETIRED

This module stores the rungs the learning pipeline decides (``STATUSES``):

    research      -> rejected       (failed the in-sample part of the backtest gate)
    research      -> backtested     (passed in sample)
    backtested    -> rejected       (failed out of sample, walk-forward or the Monte Carlo test)
    backtested    -> oos_validated  (passed all of them)
    oos_validated -> under_review   (failed the untouched final holdout: a person looks at it)
    oos_validated -> paper          (passed the holdout; now trades on paper with live candles)
    paper         -> retired        (paper results fell below the bar)

The three rungs above paper are never stored here. ``learn.ladder`` reads them from the evidence:
enough paper trades (shadow), the Phase 6 go/no-go report (live-eligible) and your approval in
``trade.golive`` (live enabled). There is no "live" status, so nothing in this module can move a
strategy to real money: that stays a human decision through the go/no-go gate and the live lock.

Files written before the ladder used "candidate" for an untested strategy; it is read as
"research".
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

DEFAULT_PATH = "data/learn/registry.json"
STATUSES = ("research", "backtested", "oos_validated", "paper", "rejected", "under_review", "retired")
LEGACY_STATUSES = {"candidate": "research"}  # older registry files
UNTESTED = ("research", "backtested", "oos_validated")  # not judged by the gate yet (or stopped midway)


def migrate_status(status: str) -> str:
    return LEGACY_STATUSES.get(status, status)


@dataclass
class Entry:
    id: str
    kind: str  # "rule" (written by the agent), "builtin" (hand-coded) or "short" (a long one's short twin)
    name: str
    status: str = "research"
    spec: dict | None = None
    source: str = ""
    added: float = field(default_factory=time.time)
    backtest: dict = field(default_factory=dict)
    paper: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def __post_init__(self):
        self.status = migrate_status(self.status)

    def set_status(self, status: str, why: str) -> None:
        status = migrate_status(status)
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
        wanted = {migrate_status(s) for s in statuses}
        return [e for e in self.entries.values() if e.status in wanted]

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

    if entry.kind == "builtin":  # an improved version carries its own settings (learn.improve)
        return STRATEGIES[entry.name](**((entry.spec or {}).get("params") or {}))
    if entry.kind == "short":
        from ..strategies.mirror import ShortMirror

        s = entry.spec or {}
        base = Entry(id=s["base_id"], kind=s["base_kind"], name=s["base_name"], spec=s.get("base_spec"))
        return ShortMirror(make_strategy(base))
    return RuleStrategy(entry.spec)


def short_twin(entry: Entry) -> Entry:
    """A new research entry that trades ``entry`` (a long strategy) short on Bybit perpetuals
    (``strategies.mirror``). It carries a copy of the long rules, so it doesn't change when the
    long one is edited or retired."""
    if entry.kind == "short":
        raise ValueError("that is already a short twin")
    return Entry(id=f"short-{entry.id}", kind="short", name=f"short: {entry.name}",
                 spec={"base_id": entry.id, "base_kind": entry.kind, "base_name": entry.name,
                       "base_spec": entry.spec},
                 source=entry.source)
