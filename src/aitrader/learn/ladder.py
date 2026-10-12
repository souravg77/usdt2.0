"""Where each strategy stands on the promotion ladder (docs/promotion-ladder.md).

The registry stores the rungs up to PAPER/DEMO and the off-ladder states. The three rungs above
paper are read from evidence other parts of the system keep, so they can't be set by editing the
registry:

- SHADOW MONITORED: the strategy has trades on Bybit demo trading (the executor in "testnet"
  mode), each compared with its paper twin (``data/trade/testnet_state.json``).
- LIVE-ELIGIBLE: the last Phase 6 go/no-go report (``trade.golive``) passed with it selected.
- LIVE ENABLED: your approval in ``golive approve`` names it and still matches its rules and the
  active AI model (``GoLive.guard``). Only that approval puts a strategy here, and the live lock in
  ``.env`` is still needed before any real order.

This module only reads; it never changes a status or an approval.
"""

from __future__ import annotations

import json
import os

LADDER = ("research", "backtested", "oos_validated", "paper", "shadow", "live_eligible", "live_enabled")
OFF_LADDER = ("rejected", "under_review", "retired")
LABELS = {
    "research": "RESEARCH", "backtested": "BACKTESTED", "oos_validated": "OUT-OF-SAMPLE VALIDATED",
    "paper": "PAPER/DEMO", "shadow": "SHADOW MONITORED", "live_eligible": "LIVE-ELIGIBLE",
    "live_enabled": "LIVE ENABLED (approved)", "rejected": "REJECTED", "under_review": "UNDER REVIEW",
    "retired": "RETIRED",
}


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class Evidence:
    """What the later gates have recorded, read once for a whole strategy list."""

    def __init__(self, golive_dir: str = "data/golive", trade_dir: str = "data/trade",
                 registry_path: str = "data/learn/registry.json", models_dir: str = "data/ai/models"):
        self.golive_dir, self.registry_path, self.models_dir = golive_dir, registry_path, models_dir
        report = _load(os.path.join(golive_dir, "report.json"))
        self.eligible = set(report.get("strategies", [])) if report.get("passed") else set()
        self.approved = set((_load(os.path.join(golive_dir, "approval.json")).get("strategies") or {}))
        st = _load(os.path.join(trade_dir, "testnet_state.json"))
        self.shadow: dict[str, dict] = {}
        for t in st.get("closed", []):
            s = self.shadow.setdefault(t.get("entry_id"), {"trades": 0, "open": 0, "vs_paper": []})
            s["trades"] += 1
            if t.get("paper_r") is not None:
                s["vs_paper"].append(t["r"] - t["paper_r"])
        for p in st.get("positions", {}).values():
            self.shadow.setdefault(p.get("entry_id"), {"trades": 0, "open": 0, "vs_paper": []})["open"] += 1
        self._guard = None

    def approval_holds(self, entry_id: str) -> bool:
        if entry_id not in self.approved:
            return False
        if self._guard is None:
            from ..trade.golive import GoLive

            self._guard = GoLive(root=self.golive_dir, registry_path=self.registry_path,
                                 models_dir=self.models_dir).guard
        try:
            return not self._guard(entry_id)
        except Exception:  # an unreadable approval or registry never counts as approved
            return False


def ladder_state(entry, evidence: Evidence | None = None) -> tuple[str, str]:
    """(state, note) for one registry entry. ``state`` is a key of ``LABELS``."""
    from ..trade.golive import passed_backtest

    if entry.status != "paper":
        return entry.status, ""
    if not passed_backtest(entry):
        return "paper", "paper-tested only: did not pass the backtest gate"
    ev = evidence or Evidence()
    if ev.approval_holds(entry.id):
        return "live_enabled", "approved in golive; the live lock in .env still applies"
    sh = ev.shadow.get(entry.id)
    note = ""
    if sh:
        vs = sh["vs_paper"]
        note = (f"demo: {sh['trades']} closed, {sh['open']} open"
                + (f", {sum(vs) / len(vs):+.3f}R per trade vs paper" if vs else ""))
    if entry.id in ev.eligible:
        return "live_eligible", note or "go/no-go gate passed; waiting for your approval"
    if sh:
        return "shadow", note
    return "paper", ""


def label(state: str) -> str:
    return LABELS.get(state, state.upper())
