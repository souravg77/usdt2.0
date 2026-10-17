"""Phase 4: the AI trade filter.

A gradient-boosting model that, for each signal a validated strategy gives, estimates the chance
it reaches 1R before its stop, skips the signal below a threshold and scales the position size
by confidence (never above the strategy's own risk). It does not invent trades.

- ``features``: market regime, volatility, trend, levels, time and streak features per signal
- ``dataset``: the training set from backtests and paper trades
- ``model``: chronological fit / threshold / test, and the gate against the unfiltered rules
- ``registry``: versioned models, human promotion, instant rollback
- ``live``: the filter as the paper trader uses it
"""

from .dataset import build_dataset, load as load_dataset, save as save_dataset
from .live import LiveFilter
from .model import FilterModel, Gate, TrainConfig, report_markdown, train
from .registry import ModelRegistry

__all__ = ["build_dataset", "load_dataset", "save_dataset", "LiveFilter", "FilterModel", "Gate",
           "TrainConfig", "report_markdown", "train", "ModelRegistry"]
