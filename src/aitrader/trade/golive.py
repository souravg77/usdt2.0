"""Phase 6: the go/no-go gate, your approval, and the guard the live executor checks.

Going live takes four separate things, and the code checks all of them:

1. The gate passes (``evaluate``): enough paper trades, positive expectancy in paper and in the
   backtest, paper drawdown under your live loss limit, paper close to the backtest, two weeks
   without a crash, and the security items confirmed (trade-only keys, kill switch tested).
2. You approve it (``approve``) for named strategies. The approval is tied to those strategies'
   rules and to the active AI model, so changing either sends trading back to paper.
3. The live lock in ``.env`` is opened by you (``safety.assert_live_allowed``), which this module
   never touches.
4. The pilot limits are set (``AITRADER_PILOT_MAX_USDT``, at most two strategies, one exchange).

``monitor`` runs on every closed live trade and returns a reason to go back to paper when live
results leave the expected range.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from ..backtest import metrics
from ..ops import EventLog

DEFAULT_DIR = "data/golive"
DAY_MS = 86_400_000


@dataclass(frozen=True)
class GateConfig:
    min_paper_trades: int = 100
    max_paper_drawdown_r: float = 10.0  # your live loss limit, in R
    max_gap_r: float = 0.15  # |paper expectancy - backtest expectancy|
    stable_days: int = 14
    max_heartbeat_gap_min: float = 30.0
    kill_test_max_age_days: int = 30
    live_min_trades_to_judge: int = 20
    live_max_shortfall_r: float = 0.3  # live expectancy this far under paper -> back to paper

    @classmethod
    def from_env(cls) -> "GateConfig":
        e = os.environ.get
        d = cls()
        return cls(min_paper_trades=int(e("AITRADER_GATE_MIN_PAPER_TRADES", d.min_paper_trades)),
                   max_paper_drawdown_r=float(e("AITRADER_GATE_MAX_DRAWDOWN_R", d.max_paper_drawdown_r)),
                   max_gap_r=float(e("AITRADER_GATE_MAX_GAP_R", d.max_gap_r)),
                   stable_days=int(e("AITRADER_GATE_STABLE_DAYS", d.stable_days)))


class _R:
    def __init__(self, r):
        self.r, self.cost_r = float(r), 0.0


def passed_backtest(entry) -> bool:
    """The strategy's latest backtest ran on data and passed the backtest gate."""
    bt = entry.backtest or {}
    return not bt.get("failed") and (bt.get("out_of_sample") or {}).get("trades", 0) > 0


class GoLive:
    def __init__(self, root: str = DEFAULT_DIR, registry_path: str = "data/learn/registry.json",
                 paper_state: str = "data/learn/paper_state.json", models_dir: str = "data/ai/models",
                 events: EventLog | None = None, cfg: GateConfig | None = None, clock=time.time):
        self.root, self.registry_path, self.paper_state = root, registry_path, paper_state
        self.models_dir, self.events = models_dir, events or EventLog()
        self.cfg, self.clock = cfg or GateConfig(), clock

    # ---- files ----------------------------------------------------------------------------------

    def _load(self, name: str) -> dict:
        try:
            with open(os.path.join(self.root, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, data: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, name)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(path + ".tmp", path)

    def attest(self, item: str, by: str, note: str = "") -> dict:
        """Record a person's confirmation of a manual security item."""
        if item not in ("keys", "kill_switch", "compliance"):
            raise ValueError("item must be keys, kill_switch or compliance")
        if not by:
            raise ValueError("say who confirms it (--by)")
        data = self._load("attestations.json")
        data[item] = {"by": by, "at": self.clock(), "note": note}
        self._save("attestations.json", data)
        return data[item]

    # ---- the gate -------------------------------------------------------------------------------

    def _strategies(self, ids: list[str] | None):
        from ..learn import Registry

        reg = Registry(self.registry_path)
        if ids:
            entries = [reg.get(i) for i in ids]
        else:  # paper trading also runs strategies that failed or skipped the backtest: not live candidates
            entries = [e for e in reg.with_status("paper") if passed_backtest(e)]
        return reg, entries

    def evaluate(self, strategy_ids: list[str] | None = None) -> dict:
        cfg, now = self.cfg, self.clock()
        _reg, entries = self._strategies(strategy_ids)
        ids = {e.id for e in entries}
        try:
            with open(self.paper_state, encoding="utf-8") as fh:
                paper = json.load(fh)
        except (OSError, ValueError):
            paper = {"trades": []}
        trades = sorted((t for t in paper.get("trades", []) if t.get("entry_id") in ids),
                        key=lambda t: t["exit_ts"])
        m = metrics([_R(t["r"]) for t in trades])
        bt = [(e.backtest.get("out_of_sample") or {}) for e in entries]
        bt_n = sum(b.get("trades", 0) for b in bt)
        bt_exp = (sum(b.get("expectancy_r", 0) * b.get("trades", 0) for b in bt) / bt_n) if bt_n else 0.0
        bt_ok = bool(entries) and all(b.get("trades", 0) and b.get("expectancy_r", 0) > 0 for b in bt)
        gap = abs(m["expectancy_r"] - bt_exp)
        stab_ok, stab_text = self._stability(now)
        att = self._load("attestations.json")
        kill_ok, kill_text = self._kill_tested(now, att)
        rows = [
            ("Strategies selected", "at least 1, status paper",
             ", ".join(sorted(ids)) or "none", bool(entries) and all(e.status == "paper" for e in entries)),
            ("Paper trades completed", f">= {cfg.min_paper_trades}", str(m["trades"]),
             m["trades"] >= cfg.min_paper_trades),
            ("Expectancy after costs (paper)", "> 0", f"{m['expectancy_r']:+.3f}R", m["expectancy_r"] > 0),
            ("Expectancy after costs (backtest, out of sample)", "> 0 for every strategy",
             f"{bt_exp:+.3f}R over {bt_n} trades", bt_ok),
            ("Max drawdown in paper", f"<= {cfg.max_paper_drawdown_r:g}R (your live loss limit)",
             f"{m['max_drawdown_r']:.1f}R", m["trades"] > 0 and m["max_drawdown_r"] <= cfg.max_paper_drawdown_r),
            ("Paper vs backtest gap", f"<= {cfg.max_gap_r:g}R", f"{gap:.3f}R", m["trades"] > 0 and gap <= cfg.max_gap_r),
            ("Stability", f"no crash and no gap over {cfg.max_heartbeat_gap_min:g} min for {cfg.stable_days} days",
             stab_text, stab_ok),
            ("Security: keys", "trade-only, no withdrawal, IP whitelist, 2FA (aitrader golive attest keys)",
             _att_text(att.get("keys")), "keys" in att),
            ("Security: kill switch tested", f"on testnet within {cfg.kill_test_max_age_days} days",
             kill_text, kill_ok),
        ]
        report = {"at": now, "strategies": sorted(ids), "config": asdict(cfg),
                  "criteria": [{"name": n, "threshold": t, "value": v, "passed": bool(p)} for n, t, v, p in rows],
                  "paper": {k: m[k] for k in ("trades", "expectancy_r", "max_drawdown_r", "win_rate")},
                  "backtest_expectancy_r": bt_exp}
        report["passed"] = all(c["passed"] for c in report["criteria"])
        self._save("report.json", report)
        return report

    def _stability(self, now: float) -> tuple[bool, str]:
        since = int((now - self.cfg.stable_days * 86400) * 1000)
        evs = self.events.read(since_ms=since - DAY_MS, kinds={"heartbeat", "crash"}, include_rotated=True)
        crashes = [e for e in evs if e["kind"] == "crash" and e["ts"] >= since]
        beats = sorted(e["ts"] for e in evs if e["kind"] == "heartbeat" and e.get("service") == "paper")
        if crashes:
            return False, f"{len(crashes)} crash(es) in the last {self.cfg.stable_days} days"
        if not beats or beats[0] > since:
            have = (now * 1000 - beats[0]) / DAY_MS if beats else 0
            return False, f"paper service has run {have:.1f} of {self.cfg.stable_days} days"
        recent = [b for b in beats if b >= since]
        points = [since] + recent + [int(now * 1000)]
        worst = max(b - a for a, b in zip(points, points[1:])) / 60000
        ok = worst <= self.cfg.max_heartbeat_gap_min
        return ok, f"longest gap {worst:.0f} min, no crashes"

    def _kill_tested(self, now: float, att: dict) -> tuple[bool, str]:
        since = int((now - self.cfg.kill_test_max_age_days * 86400) * 1000)
        kills = [e for e in self.events.read(since_ms=since, kinds={"kill"}, include_rotated=True)
                 if e.get("service") == "executor-testnet"]
        if kills:
            return True, "flattened on testnet " + time.strftime("%Y-%m-%d", time.gmtime(kills[-1]["ts"] / 1000))
        if "kill_switch" in att and att["kill_switch"]["at"] * 1000 >= since:
            return True, _att_text(att["kill_switch"])
        return False, "no testnet kill-switch run recorded"

    # ---- approval -------------------------------------------------------------------------------

    def _fingerprint(self, ids: list[str]) -> dict:
        from ..ai import ModelRegistry

        reg, entries = self._strategies(ids)
        return {"strategies": {e.id: json.dumps(e.spec, sort_keys=True) if e.spec else e.name for e in entries},
                "ai_model": ModelRegistry(self.models_dir).active}

    def approve(self, strategy_ids: list[str], by: str, max_strategies: int = 2) -> dict:
        if not by:
            raise ValueError("say who approves (--by)")
        if not strategy_ids or len(strategy_ids) > max_strategies:
            raise ValueError(f"the pilot runs 1 to {max_strategies} named strategies")
        report = self.evaluate(strategy_ids)
        if not report["passed"]:
            failed = [c["name"] for c in report["criteria"] if not c["passed"]]
            raise ValueError("the go/no-go gate does not pass: " + ", ".join(failed))
        approval = {"by": by, "at": self.clock(), "report": report, **self._fingerprint(strategy_ids)}
        self._save("approval.json", approval)
        self.events.emit("gate", f"go-live approved by {by} for {', '.join(strategy_ids)}", level="warning")
        return approval

    def revoke(self, by: str = "") -> None:
        if self._load("approval.json"):
            self._save("approval.json", {})
            self.events.emit("gate", f"go-live approval revoked{' by ' + by if by else ''}", level="warning")

    def guard(self, entry_id: str) -> list[str]:
        """Problems that block a live entry for this strategy (empty = allowed)."""
        a = self._load("approval.json")
        if not a:
            return ["no go-live approval (aitrader golive approve)"]
        if entry_id not in a["strategies"]:
            return [f"{entry_id} is not one of the approved strategies"]
        now = self._fingerprint(list(a["strategies"]))
        problems = []
        if now["strategies"].get(entry_id) != a["strategies"][entry_id]:
            problems.append(f"{entry_id} changed or left paper status since approval: back to paper")
        if now["ai_model"] != a["ai_model"]:
            problems.append("the active AI model changed since approval: back to paper")
        return problems

    def monitor(self, closed: list[dict]) -> str | None:
        """A reason to return to paper, or None. ``closed`` are live trades with ``r``."""
        live = [t for t in closed if t.get("mode") == "live"]
        if not live:
            return None
        m = metrics([_R(t["r"]) for t in live])
        if m["max_drawdown_r"] > self.cfg.max_paper_drawdown_r:
            return f"live drawdown {m['max_drawdown_r']:.1f}R passed the {self.cfg.max_paper_drawdown_r:g}R limit"
        a = self._load("approval.json")
        expected = (a.get("report") or {}).get("paper", {}).get("expectancy_r")
        if expected is not None and m["trades"] >= self.cfg.live_min_trades_to_judge and \
                m["expectancy_r"] < expected - self.cfg.live_max_shortfall_r:
            return (f"live expectancy {m['expectancy_r']:+.3f}R over {m['trades']} trades is below the "
                    f"paper {expected:+.3f}R by more than {self.cfg.live_max_shortfall_r:g}R")
        return None


def _att_text(a: dict | None) -> str:
    if not a:
        return "not confirmed"
    return f"confirmed by {a['by']} {time.strftime('%Y-%m-%d', time.gmtime(a['at']))}"
