"""The trade filter as the paper trader uses it.

``LiveFilter`` follows the registry: when a person promotes a model or rolls one back, the next
check picks up the change without a restart. With no active model every signal passes at full
size, which is the same as running without the filter.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..strategies.base import Signal
from .features import as_long, market_features, signal_features
from .registry import ModelRegistry

log = logging.getLogger(__name__)


class LiveFilter:
    def __init__(self, root: str):
        self.root = root
        self._mtime = -1.0
        self.version: str | None = None
        self.model = None

    def _refresh(self) -> None:
        reg = ModelRegistry(self.root)
        if reg.mtime() == self._mtime:
            return
        self._mtime = reg.mtime()
        try:
            loaded = reg.load_active()
        except Exception:
            log.exception("could not load the active trade-filter model; filter is off")
            loaded = None
        self.version, self.model = loaded if loaded else (None, None)
        log.info("trade filter: %s", self.version or "off (no active model)")

    def check(self, sig: Signal, strategy_id: str, df: pd.DataFrame,
              recent_r: list[float]) -> dict:
        """Decide on a signal produced on the last bar of ``df``. ``recent_r`` is the strategy's
        closed trades (oldest first). A short is judged as the long on the flipped chart
        (``features.as_long``). Returns {take, size, prob, version}."""
        self._refresh()
        if self.model is None:
            return {"take": True, "size": 1.0, "prob": None, "version": None}
        sig, df = as_long(sig, df)
        row = signal_features(sig, strategy_id, market_features(df).iloc[-1].to_dict(), recent_r)
        p = float(self.model.predict(pd.DataFrame([row]))[0])
        take, size = self.model.decide(p)
        return {"take": take, "size": size, "prob": round(p, 4), "version": self.version}
