"""Keep only the best strategies paper trading.

Every strategy can be put into paper trading (``learn paper-all``); this narrows them to the best
``n`` by their evidence, so the paper results (and later the go/no-go gate) are about strategies
worth trading. A strategy ranks higher when:

1. it passed the backtest gate (only those can ever be approved for live), then
2. its result per trade is higher, out of sample plus any paper trades, shrunk toward 0 when there
   are few trades (``PRIOR`` trades of 0R are mixed in), so 3 lucky trades don't beat 80 good ones.

A few places are kept for each chart timeframe (``PER_TIMEFRAME``, at most half of ``n`` in all), so
the kept set doesn't end up trading only 4h and daily candles, which close a few times a day. A
strategy gets one of those places only with a result above 0R per trade, after fees, both on that
timeframe alone and overall.

The top ``n`` are kept even when some score below 0 (paper money, and their results keep coming in);
the paper trader still retires one that loses too much. The rest leave paper trading with the reason
``BENCHED``; a later run can bring one back if it climbs into the top ``n``. Strategies retired
for poor paper results stay retired. Open paper positions are still managed until they close.

Each engine has its own ranking (``market``): Spot AI ranks the long strategies by their backtest
and its own paper trades, Futures AI the longs and the short twins by their backtest and its own
paper trades. An engine trades only the strategies its own ranking kept (``kept_ids``), so a
strategy that earned its place on Futures never trades on Spot because of it, and the other way
round. A strategy leaves paper trading only when neither engine kept it.
"""

from __future__ import annotations

import json
import os
import re

from .registry import Entry, Registry

PRIOR = 30
BENCHED = "not in the best"
PER_TIMEFRAME = 2  # places kept for the best strategies on each timeframe


def _passed(entry: Entry) -> bool:
    bt = entry.backtest or {}
    return not bt.get("failed") and (bt.get("out_of_sample") or {}).get("trades", 0) > 0


def score(entry: Entry, market: str | None = None) -> float | None:
    """Result per trade, out of sample and paper together, shrunk toward 0 for few trades. None
    when the strategy has no backtest result yet. With ``market`` only that engine's paper trades."""
    oos = (entry.backtest or {}).get("out_of_sample") or {}
    if not entry.backtest or "trades" not in oos:
        return None
    n_bt, e_bt = oos.get("trades", 0), oos.get("expectancy_r", 0.0)
    paper = entry.paper or {}
    if market:
        paper = (paper.get("markets") or {}).get(market) or {}
    n_p, e_p = paper.get("trades", 0), paper.get("expectancy_r", 0.0)
    return (n_bt * e_bt + n_p * e_p) / (n_bt + n_p + PRIOR)


def benched(entry: Entry) -> bool:
    """Taken out of paper trading by this ranking (not for poor paper results)."""
    return entry.status == "retired" and bool(entry.history) and \
        str(entry.history[-1].get("why", "")).startswith(BENCHED)


def rank(reg: Registry, market: str | None = None) -> list[tuple[Entry, float]]:
    """Strategies that may paper trade (in paper now, or benched earlier), best first. With
    ``market`` the ones that engine may trade (no shorts on Spot), scored on its own paper trades."""
    pool = [e for e in reg.entries.values() if (e.status == "paper" or benched(e))
            and not (e.spec or {}).get("pick")  # timeframe picks (learn.picks) are kept apart
            and not (market == "spot" and e.kind == "short")]
    scored = [(e, s) for e in pool if (s := score(e, market)) is not None]
    return sorted(scored, key=lambda es: (_passed(es[0]), es[1]), reverse=True)


def timeframe_scores(entry: Entry) -> dict[str, float]:
    """Out-of-sample result per trade on each timeframe alone, shrunk toward 0 like ``score``."""
    trades: dict[str, int] = {}
    total: dict[str, float] = {}
    for m in (entry.backtest or {}).get("markets") or []:
        tf, n = m.get("timeframe"), m.get("trades_oos", 0)
        if tf and n:
            trades[tf] = trades.get(tf, 0) + n
            total[tf] = total.get(tf, 0.0) + n * m.get("exp_oos", 0.0)
    return {tf: total[tf] / (trades[tf] + PRIOR) for tf in trades}


def _tf_order(tf: str) -> int:
    from ..models import TIMEFRAME_MS

    return TIMEFRAME_MS.get(tf, 0)


def choose(ranked: list[tuple[Entry, float]], n: int,
           per_tf: int = PER_TIMEFRAME) -> list[tuple[Entry, float, str]]:
    """The ``n`` to keep, in ranking order, each with why: first up to ``per_tf`` places for the
    best on each timeframe (shortest first), then the best overall. A timeframe place needs a result
    above 0R on that timeframe *and* overall, since the paper trader trades every timeframe of a
    strategy; at most half of the ``n`` places go this way, so the best overall always get the rest."""
    tf_scores = {e.id: timeframe_scores(e) for e, _s in ranked}
    tfs = sorted({tf for sc in tf_scores.values() for tf in sc}, key=_tf_order)
    why: dict[str, str] = {}
    for tf in tfs:
        have = sum(1 for i in why if tf in tf_scores[i])
        best = sorted(((e, tf_scores[e.id][tf]) for e, total in ranked
                       if e.id not in why and total > 0 and tf_scores[e.id].get(tf, 0.0) > 0),
                      key=lambda es: (_passed(es[0]), es[1]), reverse=True)
        for e, s in best[:max(0, per_tf - have)]:
            if len(why) >= n // 2:
                break
            why[e.id] = f"best on {tf} ({s:+.3f}R per trade there)"
    for e, _s in ranked:
        if len(why) >= n:
            break
        why.setdefault(e.id, "best overall")
    return [(e, s, why[e.id]) for e, s in ranked if e.id in why]


def keep_top(reg: Registry, n: int, per_tf: int = PER_TIMEFRAME, market: str | None = None,
             others: set | frozenset = frozenset()) -> dict:
    """Keep the best ``n`` in paper trading; bench the rest. Returns what changed. With ``market``
    only that engine's ranking: ``others`` (the other engine's kept ids) stay in paper trading."""
    ranked = rank(reg, market)
    if not ranked:
        return {"kept": [], "benched": [], "returned": [], "untested": 0}
    chosen = choose(ranked, n, per_tf)
    keep_ids = {e.id for e, _s, _w in chosen} | set(others)
    out = {"market": market, "kept": [{"id": e.id, "score": round(sc, 4), "passed": _passed(e), "why": w,
                     "timeframes": sorted(timeframe_scores(e), key=_tf_order)} for e, sc, w in chosen],
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
        if e.status == "paper" and e.id not in keep_ids and score(e) is None and not (e.spec or {}).get("pick"):
            e.set_status("retired", f"{BENCHED} {n} (no backtest result yet)")
            out["benched"].append(e.id)
    return out


# --- the indicators the kept strategies need ------------------------------------------------------

# chart indicator key (the Markets page chips) -> words in a strategy's rules that mean it uses it
INDICATOR_WORDS: dict[str, str] = {
    "ema": r"\b(ema\w*|moving averages?)\b",
    "sma": r"\b(sma\d*|\w*_sma|simple moving average)\b",
    "bb": r"\b(bb_\w+|bollinger)",
    "vwap": r"\bvwap",
    "st": r"\bsupertrend",
    "ich": r"\b(tenkan|kijun|cloud_\w+|ichimoku)",
    "psar": r"\b(psar|parabolic)",
    "sr": r"\b(support|resistance|swing (?:high|low)s?|market structure)",
    "dc": r"\b(highest_high|lowest_low|highest high|lowest low|donchian)",
    "vol": r"\b(volume|rel_volume|avg_volume)",
    "fi": r"\b(force_index|force index)",
    "obv": r"\bobv",
    "mfi": r"\b(mfi|money flow index)",
    "cmf": r"\b(cmf|chaikin)",
    "roc": r"(\bchange\(|\broc\b|rate of change)",
    "rsi": r"\brsi\w*",
    "macd": r"\bmacd",
    "adx": r"\b(adx|plus_di|minus_di)\b",
    "stoch": r"\bstoch",
    "kc": r"\b(keltner|kc_\w+)",
    "fib": r"\b(fib\w*|retracement)",
    "vp": r"\b(volume profile|value area|point of control|poc|vah|val)\b",
    "pat": r"\b(hammer|doji|engulfing|inside_bar|outside_bar|pin bar|ha_green|heikin|body_pct|nr\d*\(|nr7)",
    "atr": r"\b(atr\w*|true range)",
    "cci": r"\bcci",
    "smc": r"\b(fvg\w*|\w*_fvg|fair value gaps?|\w*_ob|bull_ob_\w+|order blocks?|sweep_\w+|liquidity sweeps?)",
    "willr": r"\b(williams|willr)",
}
# a book rule's notes (why it was coded so, what couldn't be) can name indicators it doesn't use
NOT_RULES = ("name", "description", "source", "decisions", "uncodable", "twin_of", "pick", "pick_of", "builtin")


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


def indicators_used(reg: Registry, market: str | None = None, only_winners: bool = False,
                    kept: set | None = None) -> dict[str, int]:
    """How many paper-trading strategies use each chart indicator, most used first. With ``market``
    ("spot" or "futures") only the strategies that engine trades: its own WIN strategies (all of them
    with ``only_winners``, its WIN-only switch), the ones its ranking kept, and no shorts on Spot."""
    from .picks import trades_now

    counts: dict[str, int] = {}
    for e in reg.with_status("paper"):
        if market and ((market == "spot" and e.kind == "short") or not trades_now(e, only_winners, market, kept)):
            continue
        try:
            used = indicators(e)
        except Exception:
            continue
        for k in used:
            counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --- how many to keep (remembered next to the registry) -------------------------------------------

DEFAULT_N, MIN_N, MAX_N = 15, 10, 20


def _n_path(registry_path: str, market: str | None = None) -> str:
    name = f"keep_{market}.json" if market else "keep.json"
    return os.path.join(os.path.dirname(os.path.abspath(registry_path)), name)


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def saved_n(registry_path: str, market: str | None = None) -> int:
    """How many ``market``'s engine keeps (the number used before the engines had their own, else 15)."""
    for path in ([_n_path(registry_path, market)] if market else []) + [_n_path(registry_path)]:
        try:
            return clamp(int(_read(path)["n"]))
        except (ValueError, KeyError, TypeError):
            continue
    return DEFAULT_N


def save_n(registry_path: str, n: int, market: str | None = None) -> int:
    n = clamp(n)
    path = _n_path(registry_path, market)
    data = _read(path)
    data["n"] = n
    _write(path, data)
    return n


def kept_ids(registry_path: str, market: str) -> set | None:
    """The strategies ``market``'s engine kept at its last ranking; None before its first one."""
    kept = _read(_n_path(registry_path, market)).get("kept")
    return set(kept) if isinstance(kept, list) else None


def save_kept(registry_path: str, market: str, ids) -> None:
    path = _n_path(registry_path, market)
    data = _read(path)
    data["kept"] = sorted(ids)
    _write(path, data)


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def clamp(n: int) -> int:
    return max(MIN_N, min(MAX_N, int(n)))
