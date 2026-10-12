"""Market intelligence: the regime classifier (agent G) and the pair ranking (agent I).

Both only describe the market. Neither places orders, and the regime does not block trades
unless a strategy opts in with ``avoid_regimes`` in its rule spec.
"""

from .regime import REGIMES, Regime, RegimeConfig, breakdown, classify, labels_by_close, regime_labels

__all__ = ["REGIMES", "Regime", "RegimeConfig", "breakdown", "classify", "labels_by_close",
           "regime_labels"]
