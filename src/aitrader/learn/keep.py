"""Keep only the best strategies paper trading.

Every strategy can be put into paper trading (``learn paper-all``); this narrows them to the best
``n`` by their evidence, so the paper results (and later the go/no-go gate) are about strategies
worth trading. A strategy ranks higher when:

1. it passed the backtest gate (only those can ever be approved for live), then
2. its result per trade is higher, out of sample plus any paper trades, shrunk toward 0 when there
   are few trades (``PRIOR`` trades of 0R are mixed in), so 3 lucky trades don't beat 80 good ones.

The top ``n`` are kept even when some score below 0 (paper money, and their results keep coming in);
the paper trader still retires one that loses too much. The rest leave paper trading with the reason
``BENCHED``; a later run can bring one back if it climbs into the top ``n``. Strategies retired
for poor paper results stay retired. Open paper positions are still managed until they close.
"""

from __future__ import annotations

import json
import os
import re

from .registry import Entry, Registry

PRIOR = 30
BENCHED = "not in the best"


def _passed(entry: Entry) -> bool:
    bt = entry.backtest or {}
    return not bt.get("failed") and (bt.get("out_of_sample") or {}).get("trades", 0) > 0


def score(entry: Entry) -> float | None:
    """Result per trade, out of sample and paper together, shrunk toward 0 for few trades. None
    when the strategy has no backtest result yet."""
    oos = (entry.backtest or {}).get("out_of_sample") or {}
    if not entry.backtest or "trades" not in oos:
        return None
    n_bt, e_bt = oos.get("trades", 0), oos.get("expectancy_r", 0.0)
    n_p, e_p = (entry.paper or {}).get("trades", 0), (entry.paper or {}).get("expectancy_r", 0.0)
    return (n_bt * e_bt + n_p * e_p) / (n_bt + n_p + PRIOR)


def benched(entry: Entry) -> bool:
    """Taken out of paper trading by this ranking (not for poor paper results)."""
    return entry.status == "retired" and bool(entry.history) and \
        str(entry.history[-1].get("why", "")).startswith(BENCHED)


def rank(reg: Registry) -> list[tuple[Entry, float]]:
    """Strategies that may paper trade (in paper now, or benched earlier), best first."""
    pool = [e for e in reg.entries.values() if e.status == "paper" or benched(e)]
    scored = [(e, s) for e in pool if (s := score(e)) is not None]
    return sorted(scored, key=lambda es: (_passed(es[0]), es[1]), reverse=True)


def keep_top(reg: Registry, n: int) -> dict:
    """Keep the best ``n`` in paper trading; bench the rest. Returns what changed."""
    ranked = rank(reg)
    if not ranked:
        return {"kept": [], "benched": [], "returned": [], "untested": 0}
    keep = [e for e, _s in ranked[:n]]
    keep_ids = {e.id for e in keep}
    out = {"kept": [{"id": e.id, "score": round(sc, 4), "passed": _passed(e)} for e, sc in ranked[:n]],
           "benched": [], "returned": [],
           "untested": sum(1 for e in reg.entries.values() if e.status == "paper" and score(e) is None)}
    for i, (e, s) in enumerate(ranked, 1):
        if e.id in keep_ids and e.status != "paper":
            e.set_status("paper", f"back in the best {n} (rank {i}, {s:+.3f}R per trade)")
            out["returned"].append(e.id)
        elif e.id not in keep_ids and e.status == "paper":
            e.set_status("retired", f"{BENCHED} {n} (rank {i}, {s:+.3f}R per trade)")
            out["benched"].append(e.id)
    # strategies without a backtest yet can't be ranked: they leave paper trading too
    for e in reg.entries.values():
        if e.status == "paper" and e.id not in keep_ids and score(e) is None:
            e.set_status("retired", f"{BENCHED} {n} (no backtest result yet)")
            out["benched"].append(e.id)
    return out


# --- the indicators the kept strategies need ------------------------------------------------------

# chart indicator key (the Markets page chips) -> words in a strategy's rules that mean it uses it
INDICATOR_WORDS: dict[str, str] = {
    "ema": r"\b(ema\w*|sma\d*|moving averages?)\b",
    "bb": r"\b(bb_\w+|bollinger)",
    "vwap": r"\bvwap",
    "st": r"\bsupertrend",
    "ich": r"\b(tenkan|kijun|cloud_\w+|ichimoku)",
    "psar": r"\b(psar|parabolic)",
    "sr": (r"\b(support|resistance|highest_high|lowest_low|highest high|lowest low|donchian"
           r"|swing (?:high|low)s?|market structure)"),
    "vol": r"\b(volume|rel_volume|avg_volume|obv\w*|mfi|cmf|force_index)",
    "rsi": r"\brsi\w*",
    "macd": r"\bmacd",
    "adx": r"\b(adx|plus_di|minus_di)\b",
    "stoch": r"\bstoch",
}
# a book rule's notes (why it was coded so, what couldn't be) can name indicators it doesn't use
NOT_RULES = ("name", "description", "source", "decisions", "uncodable")


def _rules_text(entry: Entry) -> str:
    if entry.kind == "short":
        s = entry.spec or {}
        return _rules_text(Entry(id=s.get("base_id", ""), kind=s.get("base_kind", "rule"),
                                 name=s.get("base_name", ""), spec=s.get("base_spec")))
    if entry.kind == "builtin":
        from ..strategies import STRATEGIES

        cls = STRATEGIES.get(entry.name)
        spec = getattr(cls, "spec", None)
        if spec is None:
            return entry.name
        return " ".join(str(getattr(spec, f, "")) for f in ("setup", "entry", "stop", "target", "filter"))
    # the rules only: a description, book quote or note can mention an indicator the rules don't use
    return json.dumps({k: v for k, v in (entry.spec or {}).items() if k not in NOT_RULES})


def indicators(entry: Entry) -> set[str]:
    """The chart indicators a strategy's rules use."""
    text = _rules_text(entry).lower()
    return {k for k, pat in INDICATOR_WORDS.items() if re.search(pat, text)}


def indicators_used(reg: Registry) -> dict[str, int]:
    """How many paper-trading strategies use each chart indicator, most used first."""
    counts: dict[str, int] = {}
    for e in reg.with_status("paper"):
        try:
            used = indicators(e)
        except Exception:
            continue
        for k in used:
            counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --- how many to keep (remembered next to the registry) -------------------------------------------

DEFAULT_N, MIN_N, MAX_N = 15, 10, 20


def _n_path(registry_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(registry_path)), "keep.json")


def saved_n(registry_path: str) -> int:
    try:
        with open(_n_path(registry_path), encoding="utf-8") as fh:
            return clamp(int(json.load(fh)["n"]))
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_N


def save_n(registry_path: str, n: int) -> int:
    n = clamp(n)
    os.makedirs(os.path.dirname(_n_path(registry_path)), exist_ok=True)
    with open(_n_path(registry_path), "w", encoding="utf-8") as fh:
        json.dump({"n": n}, fh)
    return n


def clamp(n: int) -> int:
    return max(MIN_N, min(MAX_N, int(n)))
