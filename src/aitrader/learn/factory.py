"""Strategy Factory: the agent writes new rule strategies by itself.

Book strategies are the starting library; the factory makes new ones by combining building blocks
the rule language already has (``learn.rules``). Each strategy is a **genome** of parts:

| Part | How many | Examples |
|---|---|---|
| ``trend`` | 0 or 1 | close above EMA(200), EMAs stacked up, Supertrend up, ADX with +DI above -DI |
| ``trigger`` | exactly 1 | RSI crossing up, MACD histogram crossing 0, a 20-bar breakout, a liquidity sweep, an FVG retest |
| ``confirm`` | 0 or 1 | volume 1.5x its average, money flowing in (CMF), OBV above its average |
| ``volatility`` | 0 or 1 | ATR not at an extreme, a Bollinger squeeze before the move |
| ``stop`` | 1 | ATR multiple or the lowest low of N bars |
| ``exit`` | 1 | a target in R, or a trailing exit; always with a time stop |
| ``timeframe`` | 1 | 15m, 30m, 1h or 4h |

The genome is turned into an ordinary rule spec and checked by ``validate_spec``, so the factory
can only write what the safe rule language accepts: long entries with a stop (Futures AI trades
them short through their short twins). Nothing is executed as code.

Every strategy carries a ``lineage``: its genome, its **family** (which blocks it uses, on which
timeframe, ignoring the numbers), its generation, its parents and how it was made. The family is
what the luck correction (``learn.luck``) counts.

A made strategy starts at RESEARCH and goes through the same gate as every other one, plus the
stress test and the raised luck bar. Most will fail; that is the point of testing them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random

from .registry import Entry, Registry, short_twin
from .rules import RuleError, spec_id, validate_spec
from . import luck

TIMEFRAMES = ("15m", "30m", "1h", "4h")


def _c(left, op, right) -> dict:
    return {"left": left, "op": op, "right": right}


# name -> (role, parameter choices, conditions builder, style). The style says what kind of move
# the block looks for, so the factory pairs a trend filter with a with-trend trigger and a
# mean-reversion trigger with an extreme rather than with a breakout filter.
BLOCKS: dict[str, tuple[str, dict, object, str]] = {
    # trend filters
    "above_ema": ("trend", {"n": [50, 100, 200]}, lambda p: [_c("close", ">", f"ema({p['n']})")], "any"),
    "ema_stack": ("trend", {}, lambda p: [_c("ema_stack(20,50,200)", ">", 0)], "with"),
    "supertrend_up": ("trend", {"n": [10, 14], "m": [2, 3]},
                      lambda p: [_c(f"supertrend_dir({p['n']},{p['m']})", ">", 0)], "with"),
    "adx_bull": ("trend", {"x": [20, 25, 30]},
                 lambda p: [_c("adx(14)", ">", p["x"]), _c("plus_di(14)", ">", "minus_di(14)")], "with"),
    "above_cloud": ("trend", {}, lambda p: [_c("close", ">", "cloud_top")], "with"),
    "higher_highs": ("trend", {}, lambda p: [_c("trend_structure", ">", 0)], "with"),
    # entry triggers
    "rsi_cross": ("trigger", {"n": [7, 14], "x": [30, 35, 40, 50]},
                  lambda p: [_c(f"rsi({p['n']})", "crosses_above", p["x"])], "revert"),
    "macd_cross": ("trigger", {}, lambda p: [_c("macd_hist", "crosses_above", 0)], "with"),
    "bb_breakout": ("trigger", {"m": [2, 2.5]}, lambda p: [_c("close", "crosses_above", f"bb_upper(20,{p['m']})")], "with"),
    "bb_reclaim": ("trigger", {"m": [2, 2.5]}, lambda p: [_c("close", "crosses_above", f"bb_lower(20,{p['m']})")], "revert"),
    "range_breakout": ("trigger", {"n": [10, 20, 55]}, lambda p: [_c("close", ">", f"highest_high({p['n']})")], "with"),
    "stoch_cross": ("trigger", {"x": [20, 30]}, lambda p: [_c("stoch_k(14,3)", "crosses_above", p["x"])], "revert"),
    "vwap_reclaim": ("trigger", {"n": [20, 50]}, lambda p: [_c("close", "crosses_above", f"vwap({p['n']})")], "with"),
    "ema_reclaim": ("trigger", {"n": [9, 20]}, lambda p: [_c("close", "crosses_above", f"ema({p['n']})")], "with"),
    "fvg_retest": ("trigger", {}, lambda p: [_c("in_bull_fvg(50)", ">", 0)], "with"),
    "order_block_tap": ("trigger", {}, lambda p: [_c("in_bull_ob(10)", ">", 0)], "with"),
    "liquidity_sweep": ("trigger", {"n": [10, 20]}, lambda p: [_c(f"sweep_low({p['n']})", ">", 0)], "revert"),
    "engulfing": ("trigger", {}, lambda p: [_c("bullish_engulfing", ">", 0)], "revert"),
    "hammer": ("trigger", {}, lambda p: [_c("hammer", ">", 0)], "revert"),
    "cci_cross": ("trigger", {}, lambda p: [_c("cci(20)", "crosses_above", -100)], "revert"),
    "williams_cross": ("trigger", {}, lambda p: [_c("williams_r(14)", "crosses_above", -80)], "revert"),
    "rsi_divergence": ("trigger", {}, lambda p: [_c("rsi_bull_div(14,30)", ">", 0)], "revert"),
    "mfi_cross": ("trigger", {"x": [20, 30]}, lambda p: [_c("mfi(14)", "crosses_above", p["x"])], "revert"),
    # confirmations
    "volume_surge": ("confirm", {"x": [1.2, 1.5, 2.0]}, lambda p: [_c("rel_volume(20)", ">", p["x"])], "any"),
    "money_inflow": ("confirm", {}, lambda p: [_c("cmf(20)", ">", 0)], "any"),
    "obv_rising": ("confirm", {}, lambda p: [_c("obv", ">", "obv_sma(20)")], "any"),
    "green_heikin_ashi": ("confirm", {}, lambda p: [_c("ha_green", ">", 0)], "any"),
    "strong_body": ("confirm", {"x": [0.5, 0.6]}, lambda p: [_c("body_pct", ">", p["x"])], "any"),
    # volatility filters
    "calm_atr": ("volatility", {"x": [0.7, 0.8, 0.9]}, lambda p: [_c("atr_pct(14)", "<", p["x"])], "any"),
    "squeeze": ("volatility", {"x": [0.2, 0.3]}, lambda p: [_c("bb_width_rank(20,2,120)", "<", p["x"])], "with"),
    "stretched": ("volatility", {"x": [-1.5, -2.0, -2.5]}, lambda p: [_c("sma_dev_atr(20,14)", "<", p["x"])], "revert"),
}
OPTIONAL_ROLES = ("trend", "confirm", "volatility")
ROLES = ("trend", "trigger", "confirm", "volatility")
STOPS = {"atr": {"mult": [1.5, 2.0, 2.5, 3.0]}, "lowest_low": {"n": [5, 10, 20]}}
TARGETS = [1.0, 1.5, 2.0, 3.0]
TRAILS = ["close<ema20", "close<psar", "rsi>70"]
TIME_STOPS = [24, 48, 96]
P_OPTIONAL = {"trend": 0.7, "confirm": 0.5, "volatility": 0.35}


def blocks_for(role: str) -> list[str]:
    return [k for k, v in BLOCKS.items() if v[0] == role]


def _pick_params(rng: random.Random, name: str) -> dict:
    return {k: rng.choice(v) for k, v in BLOCKS[name][1].items()}


def _fits(name: str, trigger: str) -> bool:
    """A filter whose style clashes with the trigger's (a squeeze filter with a dip-buying
    trigger, a 'stretched below average' filter with a breakout) is not paired with it."""
    style, t_style = BLOCKS[name][3], BLOCKS[trigger][3]
    return style == "any" or t_style == "any" or style == t_style or (BLOCKS[name][0] == "trend" and t_style == "revert")


def random_part(rng: random.Random, role: str, trigger: str | None = None) -> dict | None:
    names = [n for n in blocks_for(role) if trigger is None or _fits(n, trigger)]
    if not names:
        return None
    name = rng.choice(names)
    return {"block": name, "params": _pick_params(rng, name)}


def random_stop(rng: random.Random) -> dict:
    kind = rng.choice(sorted(STOPS))
    return {"type": kind, **{k: rng.choice(v) for k, v in STOPS[kind].items()}}


def random_exit(rng: random.Random) -> dict:
    ex = {"time_stop_bars": rng.choice(TIME_STOPS)}
    if rng.random() < 0.7:
        ex["target_r"] = rng.choice(TARGETS)
    else:
        ex["trailing"] = rng.choice(TRAILS)
    return ex


def random_genome(rng: random.Random, timeframes=TIMEFRAMES) -> dict:
    trigger = random_part(rng, "trigger")
    g = {"trigger": trigger, "stop": random_stop(rng), "exit": random_exit(rng),
         "timeframe": rng.choice(list(timeframes))}
    for role in OPTIONAL_ROLES:
        g[role] = random_part(rng, role, trigger["block"]) if rng.random() < P_OPTIONAL[role] else None
    g["avoid_downtrend"] = BLOCKS[trigger["block"]][3] == "with" and rng.random() < 0.5
    return g


def family(g: dict) -> str:
    """Which blocks, stop kind, exit kind and timeframe: everything but the numbers."""
    parts = [f"{r}:{g[r]['block']}" for r in ROLES if g.get(r)]
    parts += [f"stop:{g['stop']['type']}", "exit:" + ("target" if "target_r" in g["exit"] else "trail"),
              f"tf:{g['timeframe']}"]
    if g.get("avoid_downtrend"):
        parts.append("avoid:trend-down")
    return "|".join(parts)


def to_spec(g: dict, lineage: dict) -> dict:
    """The genome as a validated rule spec, with its lineage attached."""
    entry = []
    for role in ROLES:
        part = g.get(role)
        if part:
            entry += BLOCKS[part["block"]][2](part["params"])
    stop = dict(g["stop"])
    ex = g["exit"]
    used = [g[r]["block"] for r in ROLES if g.get(r)]
    digest = hashlib.sha256(json.dumps(g, sort_keys=True).encode()).hexdigest()[:5]
    spec = {
        "name": f"nx_{g['trigger']['block']}_{g['timeframe']}_{digest}",
        "description": "Made by the strategy factory: " + " + ".join(used),
        "source": {"book": "NEXUS strategy factory", "pages": f"generation {lineage.get('generation', 0)}",
                   "quote": ""},
        "timeframes": [g["timeframe"]],
        "entry": entry,
        "stop": stop,
        "target": {"r": ex["target_r"]} if "target_r" in ex else None,
        "exit": {"trailing": ex.get("trailing"), "time_stop_bars": ex["time_stop_bars"]},
        "decisions": [f"{r}: {g[r]['block']} {g[r]['params']}" for r in ROLES if g.get(r)],
    }
    if g.get("avoid_downtrend"):
        spec["avoid_regimes"] = ["trend-down"]
    clean = validate_spec(spec)
    clean["lineage"] = {**lineage, "genome": copy.deepcopy(g), "family": family(g)}
    return clean


def make_entry(g: dict, lineage: dict) -> Entry:
    spec = to_spec(g, lineage)
    return Entry(id=spec_id(spec), kind="rule", name=spec["name"], spec=spec,
                 source=f"factory ({lineage.get('op', 'random')}, generation {lineage.get('generation', 0)})")


def add_entries(reg: Registry, made: list[Entry], shorts: bool) -> list[Entry]:
    """Add new entries (and their short twins) to the registry; returns those that were new.
    Each one records how many families had been tried when it was made (``lineage.trials``), so its
    luck bar is never lower than that even if the registry is later cleaned."""
    added = []
    for e in made:
        if e.id in reg.entries:
            continue
        reg.add(e)
        added.append(e)
        if shorts:
            tw = short_twin(e)
            if reg.add(tw):
                added.append(tw)
    n = luck.families(reg.entries.values())
    for e in added:
        lin = luck.lineage_of(e.spec)
        lin["trials"] = max(int(lin.get("trials", 0)), n)
    return added


def generate(reg: Registry, n: int, seed: int | None = None, timeframes=TIMEFRAMES,
             shorts: bool = False, max_per_family: int = 2, max_tries: int | None = None) -> list[Entry]:
    """Make ``n`` new strategies (plus short twins with ``shorts``) and add them to ``reg`` as
    RESEARCH. A family already in the registry ``max_per_family`` times is skipped, so one lucky
    idea can't fill the list with copies of itself."""
    rng = random.Random(seed)
    per_family: dict[str, int] = {}
    for e in reg.entries.values():
        f = luck.family_of(e.spec)
        if f:
            per_family[f] = per_family.get(f, 0) + 1
    made: list[Entry] = []
    tries = 0
    while len(made) < n and tries < (max_tries or n * 50):
        tries += 1
        g = random_genome(rng, timeframes)
        f = family(g)
        if per_family.get(f, 0) >= max_per_family:
            continue
        try:
            e = make_entry(g, {"generation": 0, "parents": [], "op": "random", "seed": seed})
        except RuleError:
            continue
        if e.id in reg.entries or any(m.id == e.id for m in made):
            continue
        per_family[f] = per_family.get(f, 0) + 1
        made.append(e)
    return add_entries(reg, made, shorts)
