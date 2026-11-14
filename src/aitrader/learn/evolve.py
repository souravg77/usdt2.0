"""Evolution: breed new strategies from the factory's best ones.

Parents are machine-made strategies (``learn.factory``) that have been backtested. They are ranked
by their **in-sample** result only (R per trade after costs, shrunk toward 0 by ``PRIOR`` trades
of 0R, at least ``MIN_TRADES`` trades). Out-of-sample, walk-forward and holdout results are never
used to choose parents: if they were, the children would be fitted to the data that is supposed to
judge them, and passing the gate would mean nothing.

Children are made two ways:

- **mutation**: one change to one parent: a number moved to a neighbouring value, a block swapped
  for another of the same kind, a filter added or dropped, a different stop, exit or timeframe;
- **crossover**: each part (trend, trigger, confirmation, volatility filter, stop, exit,
  timeframe) taken from one of two parents.

Every child records its parents, how it was made and its generation (one more than its oldest
parent). The parents are never changed or removed. A child starts at RESEARCH and must pass the
full gate, the stress test and the luck bar like any other strategy; a child that changes only
numbers stays in its parent's family, one that combines new blocks is a new family and raises the
luck bar for everyone.
"""

from __future__ import annotations

import copy
import random

from . import luck
from .factory import (BLOCKS, OPTIONAL_ROLES, ROLES, STOPS, TARGETS, TIME_STOPS, TIMEFRAMES,
                      _fits, add_entries, blocks_for, family, make_entry, random_exit, random_part,
                      random_stop)
from .registry import Entry, Registry
from .rules import RuleError

MIN_TRADES = 30
PRIOR = 20
TOURNAMENT = 3


def fitness(entry: Entry) -> float | None:
    """In-sample R per trade, shrunk toward 0; None if not tested or too few trades."""
    m = (entry.backtest or {}).get("in_sample") or {}
    n = int(m.get("trades", 0))
    if n < MIN_TRADES:
        return None
    return float(m.get("expectancy_r", 0.0)) * n / (n + PRIOR)


def parents(reg: Registry, top: int = 20) -> list[tuple[Entry, float]]:
    """The best ``top`` long machine-made strategies with a positive in-sample fitness."""
    pool = []
    for e in reg.entries.values():
        g = ((e.spec or {}).get("lineage") or {}).get("genome")
        f = fitness(e)
        if g and e.kind == "rule" and f is not None and f > 0:
            pool.append((e, f))
    pool.sort(key=lambda x: -x[1])
    return pool[:top]


def _neighbour(rng: random.Random, choices: list, value):
    if value not in choices:
        return rng.choice(choices)
    i = choices.index(value)
    near = [choices[j] for j in (i - 1, i + 1) if 0 <= j < len(choices)]
    return rng.choice(near) if near else value


def mutate(rng: random.Random, g: dict, timeframes=TIMEFRAMES) -> dict:
    g = copy.deepcopy(g)
    trig = g["trigger"]["block"]
    moves = ["number", "swap", "toggle", "stop", "exit", "timeframe"]
    move = rng.choice(moves)
    if move == "number":
        tunable = [r for r in ROLES if g.get(r) and BLOCKS[g[r]["block"]][1]]
        if tunable:
            r = rng.choice(tunable)
            p = g[r]["params"]
            k = rng.choice(sorted(p))
            p[k] = _neighbour(rng, BLOCKS[g[r]["block"]][1][k], p[k])
        else:
            move = "stop"
    if move == "swap":
        role = rng.choice([r for r in ROLES if g.get(r)])
        new = random_part(rng, role, None if role == "trigger" else trig)
        if new:
            g[role] = new
        if role == "trigger":  # drop filters that no longer fit the new trigger
            for r in OPTIONAL_ROLES:
                if g.get(r) and not _fits(g[r]["block"], g["trigger"]["block"]):
                    g[r] = None
    elif move == "toggle":
        role = rng.choice(list(OPTIONAL_ROLES))
        g[role] = None if g.get(role) else random_part(rng, role, trig)
    if move == "stop":
        st = g["stop"]
        if rng.random() < 0.5:
            g["stop"] = random_stop(rng)
        else:
            for k, choices in STOPS[st["type"]].items():
                st[k] = _neighbour(rng, choices, st.get(k))
    elif move == "exit":
        ex = g["exit"]
        r = rng.random()
        if r < 0.4 and "target_r" in ex:
            ex["target_r"] = _neighbour(rng, TARGETS, ex["target_r"])
        elif r < 0.7:
            ex["time_stop_bars"] = _neighbour(rng, TIME_STOPS, ex["time_stop_bars"])
        else:
            g["exit"] = random_exit(rng)
    elif move == "timeframe":
        g["timeframe"] = _neighbour(rng, list(timeframes), g["timeframe"])
    return g


def crossover(rng: random.Random, a: dict, b: dict) -> dict:
    child = {k: copy.deepcopy(rng.choice((a, b)).get(k)) for k in
             ("trigger", "stop", "exit", "timeframe", "avoid_downtrend", *OPTIONAL_ROLES)}
    for r in OPTIONAL_ROLES:
        if child.get(r) and not _fits(child[r]["block"], child["trigger"]["block"]):
            child[r] = None
    if child.get("avoid_downtrend") and BLOCKS[child["trigger"]["block"]][3] != "with":
        child["avoid_downtrend"] = False
    return child


def _pick(rng: random.Random, pool: list[tuple[Entry, float]]) -> Entry:
    """Tournament selection: the fitter of a few drawn at random."""
    group = rng.sample(pool, min(TOURNAMENT, len(pool)))
    return max(group, key=lambda x: x[1])[0]


def breed(reg: Registry, n: int, seed: int | None = None, crossover_share: float = 0.5,
          shorts: bool = False, top: int = 20, timeframes=TIMEFRAMES) -> list[Entry]:
    """Make ``n`` children of the fittest machine-made strategies and add them to ``reg`` as
    RESEARCH. Returns the new entries (none when fewer than two parents qualify)."""
    pool = parents(reg, top)
    if len(pool) < 2:
        return []
    rng = random.Random(seed)
    made: list[Entry] = []
    tries = 0
    while len(made) < n and tries < n * 50:
        tries += 1
        if rng.random() < crossover_share:
            pa, pb = _pick(rng, pool), _pick(rng, pool)
            if pa.id == pb.id:
                continue
            ga, gb = (p.spec["lineage"]["genome"] for p in (pa, pb))
            g, op, ps = crossover(rng, ga, gb), "crossover", [pa, pb]
        else:
            pa = _pick(rng, pool)
            g, op, ps = mutate(rng, pa.spec["lineage"]["genome"], timeframes), "mutation", [pa]
        if any(g == p.spec["lineage"]["genome"] for p in ps):
            continue  # an unchanged copy of a parent
        gen = 1 + max(int(p.spec["lineage"].get("generation", 0)) for p in ps)
        try:
            e = make_entry(g, {"generation": gen, "parents": [p.id for p in ps], "op": op, "seed": seed})
        except RuleError:
            continue
        if e.id in reg.entries or any(m.id == e.id for m in made):
            continue
        made.append(e)
    return add_entries(reg, made, shorts)


def family_tree(reg: Registry, entry_id: str, depth: int = 6) -> list[str]:
    """Lines describing a strategy's ancestors, newest first, for ``learn lineage``."""
    lines: list[str] = []

    def walk(eid: str, level: int) -> None:
        e = reg.entries.get(eid)
        if e is None:
            lines.append("  " * level + f"{eid} (no longer in the registry)")
            return
        lin = luck.lineage_of(e.spec)
        fit = fitness(e)
        fit_s = f", in-sample {fit:+.3f}R" if fit is not None else ""
        lines.append("  " * level + f"{e.id}  [{e.status}] gen {lin.get('generation', '?')} "
                     f"{lin.get('op', '')}{fit_s}")
        if level < depth:
            for p in lin.get("parents", []):
                walk(p, level + 1)

    walk(entry_id, 0)
    return lines


__all__ = ["breed", "crossover", "family", "family_tree", "fitness", "mutate", "parents", "blocks_for"]
