"""Luck correction for machine-made strategies.

The backtest gate asks every strategy for an out-of-sample t-statistic of at least 2: a result that
luck alone gets past about 1 time in 40. That is fair for a strategy written down from a book
before it was tested. It is not fair for strategies the agent makes itself (``learn.factory``,
``learn.evolve``): try a thousand random rule sets and about 25 will clear t = 2 by luck, with no
edge at all.

So the bar for a machine-made strategy rises with the number of ideas tried. With ``N`` tried and
a 5% chance allowed that *any* of them gets through by luck (one-sided, Bonferroni), the
out-of-sample t has to reach ``z(1 - 0.05 / N)``:

    N ideas tried     1     10     50    100    500   1000   5000
    t needed        2.00  2.58   3.09   3.29   3.72   3.89   4.26

``N`` counts *families*, not single strategies: one family is one set of rule building blocks
(``EMA trend + RSI cross + volume``, with an ATR stop); versions of it that differ only in their
numbers count once, because their results move together. Every generated family is counted the
moment it is made, tested or not, so leaving the losers untested cannot lower the bar.

Book and hand-coded strategies keep the plain bar of 2. Nothing here can let a strategy through
that the normal gate would reject: the bar only goes up.
"""

from __future__ import annotations

from statistics import NormalDist

ALPHA = 0.05  # chance allowed that any machine-made strategy passes by luck alone
FLOOR_T = 2.0  # never below the normal gate's bar


def required_t(trials: int, alpha: float = ALPHA, floor: float = FLOOR_T) -> float:
    """The out-of-sample t-statistic a machine-made strategy needs after ``trials`` families."""
    n = max(int(trials), 1)
    return max(floor, NormalDist().inv_cdf(1 - alpha / n))


def lineage_of(spec: dict | None) -> dict:
    """A machine-made strategy's lineage; a short twin carries its long version's in ``base_spec``."""
    spec = spec or {}
    return spec.get("lineage") or (spec.get("base_spec") or {}).get("lineage") or {}


def is_machine_made(spec: dict | None) -> bool:
    return bool(lineage_of(spec))


def family_of(spec: dict | None) -> str | None:
    """The family a machine-made strategy belongs to; a short twin is a family of its own (it
    trades the other side, so its results don't move with the long one's)."""
    fam = lineage_of(spec).get("family")
    if fam and not (spec or {}).get("lineage"):
        fam += ":short"
    return fam


def families(entries) -> int:
    """How many machine-made families the registry holds (``entries``: registry entries)."""
    return len({f for e in entries if (f := family_of(getattr(e, "spec", None)))})
