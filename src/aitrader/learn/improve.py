"""The strategy improver: try many settings for each strategy, keep a change only when it also
wins on data it never saw, and let the improved version replace the old one only after it passes
the full backtest gate.

For one registry entry:

1. **Settings to try.** A hand-coded strategy's numeric settings (indicator lengths, thresholds,
   stop and target multiples; risk and safety caps are never touched), or a book rule's stop
   (ATR multiple and length, percent or lowest-low length), target in R and time stop. Short twins
   are tuned through their long rules.
2. **Search on older data.** Starting from the current settings, each setting is moved down and
   up (``STEPS``) one at a time on the in-sample part (the first 60% of every market) and the best
   move is kept; then again from there, up to ``MAX_EVALS`` backtests. A result is the R per trade
   after costs, shrunk toward 0 (``PRIOR`` trades of 0R mixed in) and only counted with at least
   ``MIN_TRADES`` trades, so a setting that trades rarely and luckily doesn't win.
3. **Check on newer data.** The best settings and the current ones are both run on the
   out-of-sample part (the next 20%), which the search never saw. The change is kept only if it
   beat the current settings there by ``MIN_GAIN`` R per trade over at least ``MIN_TRADES``
   trades, and made money.
4. **Full gate.** The improved version is added to the registry as a new strategy
   (``<id>~<hash>``, named "... (improved)") and goes through the same backtest gate as every
   other strategy: in sample, out of sample, walk-forward, Monte Carlo and, last, the untouched
   final 20%. Only if it reaches paper trading is the old version retired ("replaced by the
   improved version"); otherwise the new one is dropped and the old one keeps trading.

Every result goes to ``data/learn/improve.json`` for the Teacher page. Live trading is never
changed: a live strategy is frozen by its go-live approval, and an improved version starts on
paper like any other. A better backtest doesn't promise better trading.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time

from ..backtest import BacktestConfig, metrics, run_backtest, split_periods
from ..indicators import candles_to_frame
from .lab import MIN_BARS, evaluate, with_context
from .registry import Entry, Registry, make_strategy

log = logging.getLogger(__name__)

DEFAULT_REPORT = "data/learn/improve.json"
STEPS = (0.75, 1.25)
MAX_EVALS = 30
MIN_TRADES = 30
PRIOR = 20
MIN_GAIN = 0.03  # R per trade, on the out-of-sample part
# settings that are about safety or bookkeeping, not about reading the market
FIXED = {"risk_pct", "clean_bars", "atr_pct_max", "symbols", "session_start", "min_train", "retrain",
         "max_entry_minutes"}


def _round_like(old, new):
    if isinstance(old, bool):
        return old
    if isinstance(old, int):
        return max(2, int(round(new))) if old >= 2 else max(1, int(round(new)))
    return round(float(new), 6)


def tunables(entry: Entry) -> dict:
    """{name: current value} of the settings the improver may move."""
    if entry.kind == "short":
        s = entry.spec or {}
        return tunables(Entry(id=s["base_id"], kind=s["base_kind"], name=s["base_name"], spec=s.get("base_spec")))
    if entry.kind == "builtin":
        from ..strategies import STRATEGIES

        params = {**STRATEGIES[entry.name].default_params(), **((entry.spec or {}).get("params") or {})}
        return {k: v for k, v in params.items()
                if k not in FIXED and isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0}
    rule = entry.spec or {}
    out = {}
    stop = rule.get("stop") or {}
    if stop.get("type") == "atr":
        out["stop.mult"], out["stop.n"] = float(stop.get("mult", 2.0)), int(stop.get("n", 14))
    elif stop.get("type") == "percent":
        out["stop.pct"] = float(stop["pct"])
    elif stop.get("type") == "lowest_low":
        out["stop.n"] = int(stop.get("n", 10))
    if rule.get("target"):
        out["target.r"] = float(rule["target"]["r"])
    if (rule.get("exit") or {}).get("time_stop_bars"):
        out["exit.time_stop_bars"] = int(rule["exit"]["time_stop_bars"])
    return out


def with_settings(entry: Entry, settings: dict) -> Entry:
    """A copy of ``entry`` with ``settings`` applied (not registered)."""
    if entry.kind == "short":
        s = copy.deepcopy(entry.spec or {})
        base = with_settings(Entry(id=s["base_id"], kind=s["base_kind"], name=s["base_name"],
                                   spec=s.get("base_spec")), settings)
        s["base_spec"] = base.spec
        return Entry(id=entry.id, kind="short", name=entry.name, spec=s, source=entry.source)
    if entry.kind == "builtin":
        spec = copy.deepcopy(entry.spec or {})
        spec["params"] = {**(spec.get("params") or {}), **settings}
        return Entry(id=entry.id, kind="builtin", name=entry.name, spec=spec, source=entry.source)
    spec = copy.deepcopy(entry.spec or {})
    for key, value in settings.items():
        part, field_ = key.split(".")
        if spec.get(part) is not None:
            spec[part][field_] = value
    return Entry(id=entry.id, kind=entry.kind, name=entry.name, spec=spec, source=entry.source)


def _score(m: dict) -> float:
    if m["trades"] < MIN_TRADES:
        return float("-inf")
    return m["expectancy_r"] * m["trades"] / (m["trades"] + PRIOR)


class Markets:
    """Each stored market's in-sample and out-of-sample frames, loaded once."""

    def __init__(self, store, exchanges: list[str], symbols: list[str]):
        self.store, self.exchanges, self.symbols = store, exchanges, symbols
        self._cache: dict = {}

    def parts(self, ex: str, sym: str, tf: str):
        key = (ex, sym, tf)
        if key not in self._cache:
            candles = self.store.load(ex, sym, tf)
            if len(candles) < MIN_BARS:
                self._cache[key] = None
            else:
                ins, oos, _final = split_periods(candles_to_frame(candles))
                self._cache[key] = (ins, oos)
        return self._cache[key]

    def run(self, entry: Entry, part: int, cfg: BacktestConfig) -> dict:
        """Pooled metrics of ``entry`` on every market: ``part`` 0 = in sample, 1 = out of sample."""
        strat = make_strategy(entry)
        trades = []
        for ex in self.exchanges:
            for sym in self.symbols:
                for tf in strat.spec.timeframes:
                    p = self.parts(ex, sym, tf)
                    if p is None:
                        continue
                    df = with_context(strat, p[part], self.store, ex, sym, tf)
                    trades += run_backtest(strat, df, ex, sym, tf, cfg)
        return metrics(trades)


def search(entry: Entry, markets: Markets, cfg: BacktestConfig, max_evals: int = MAX_EVALS) -> dict:
    """Coordinate search on the in-sample part. Returns {"settings", "start", "best", "tried"}."""
    current = tunables(entry)
    start = markets.run(entry, 0, cfg)
    best_settings, best_m, best_s = {}, start, _score(start)
    tried, seen = 1, {json.dumps({}, sort_keys=True)}
    improved = True
    while improved and tried < max_evals:
        improved = False
        round_best = None
        for name, value in current.items():
            for step in STEPS:
                new = _round_like(value, (best_settings.get(name, value)) * step)
                trial = {**best_settings, name: new}
                if new == best_settings.get(name, value):
                    continue
                key = json.dumps(trial, sort_keys=True)
                if key in seen or tried >= max_evals:
                    continue
                seen.add(key)
                tried += 1
                try:
                    m = markets.run(with_settings(entry, trial), 0, cfg)
                except Exception as exc:  # a setting the strategy can't run with
                    log.info("improve %s: %s failed: %s", entry.id, trial, exc)
                    continue
                s = _score(m)
                if s > best_s and (round_best is None or s > round_best[2]):
                    round_best = (trial, m, s)
        if round_best is not None:
            best_settings, best_m, best_s = round_best
            improved = True
    return {"settings": best_settings, "start": start, "best": best_m, "tried": tried}


def _m(m: dict) -> dict:
    return {k: round(float(m[k]), 4) if isinstance(m[k], float) else m[k]
            for k in ("trades", "expectancy_r", "win_rate", "profit_factor", "max_drawdown_r")}


def improve_one(entry: Entry, reg: Registry, markets: Markets, cfg: BacktestConfig,
                exchanges: list[str], symbols: list[str], max_evals: int = MAX_EVALS) -> dict:
    """Search, check, and (if it holds up) register the improved version. Changes ``reg``."""
    out = {"strategy": entry.id, "name": entry.name, "tried": 0, "changes": {}, "outcome": "", "new_id": None}
    current = tunables(entry)
    if not current:
        out["outcome"] = "nothing to tune: its rules have no numeric settings"
        return out
    found = search(entry, markets, cfg, max_evals)
    out["tried"] = found["tried"]
    out["in_sample"] = {"before": _m(found["start"]), "after": _m(found["best"])}
    if not found["settings"]:
        out["outcome"] = "no better settings found on the older data"
        return out
    out["changes"] = {k: [current[k], v] for k, v in found["settings"].items()}
    candidate = with_settings(entry, found["settings"])
    before, after = markets.run(entry, 1, cfg), markets.run(candidate, 1, cfg)
    out["out_of_sample"] = {"before": _m(before), "after": _m(after)}
    if after["trades"] < MIN_TRADES or after["expectancy_r"] <= 0 or \
            after["expectancy_r"] < before["expectancy_r"] + MIN_GAIN:
        out["outcome"] = "better on the older data but not on the newer data it never saw: kept as it was"
        return out
    digest = hashlib.sha1(json.dumps(candidate.spec, sort_keys=True, default=str).encode()).hexdigest()[:6]
    # a hand-coded strategy's name is its code's name, so it stays; the ~hash in the id marks it
    name = entry.name if candidate.kind == "builtin" or entry.name.endswith("(improved)") \
        else f"{entry.name} (improved)"
    new = Entry(id=f"{entry.id.split('~')[0]}~{digest}", kind=candidate.kind, name=name,
                spec=candidate.spec, source=entry.source)
    if new.id in reg.entries:
        out["outcome"] = f"this improvement was already found before ({new.id})"
        return out
    evaluate(new, markets.store, exchanges, symbols, cfg)
    out["new_id"] = new.id
    if new.status != "paper":
        out["outcome"] = "failed the full backtest gate: " + "; ".join(new.backtest.get("failed", []))[:300]
        out["new_id"] = None
        return out
    new.history.append({"at": time.time(), "from": "", "to": "paper",
                        "why": f"improved version of {entry.id}: " + ", ".join(
                            f"{k} {a:g} -> {b:g}" for k, (a, b) in out["changes"].items())})
    reg.add(new)
    entry.set_status("retired", f"replaced by the improved version {new.id}")
    out["outcome"] = "improved: the new version passed the full gate and replaces the old one in paper trading"
    return out


def improve(store, reg: Registry, entries: list[Entry], exchanges: list[str], symbols: list[str],
            cfg: BacktestConfig | None = None, max_evals: int = MAX_EVALS,
            report_path: str = DEFAULT_REPORT, clock=time.time) -> dict:
    cfg = cfg or BacktestConfig()
    markets = Markets(store, exchanges, symbols)
    results = []
    for e in entries:
        try:
            results.append(improve_one(e, reg, markets, cfg, exchanges, symbols, max_evals))
        except Exception as exc:  # one broken strategy must not stop the rest
            log.exception("improve %s failed", e.id)
            results.append({"strategy": e.id, "name": e.name, "outcome": f"error: {exc}", "changes": {},
                            "tried": 0, "new_id": None})
    report = {"at": clock(), "results": results,
              "improved": sum(1 for r in results if r.get("new_id"))}
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    os.replace(report_path + ".tmp", report_path)
    return report


def load_report(path: str = DEFAULT_REPORT) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}
