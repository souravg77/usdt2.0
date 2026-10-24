"""A machine-learning strategy that finds its own entries: no hand-written entry rule.

``ml_boosted`` trains a gradient-boosted tree model (scikit-learn's HistGradientBoosting) on the
chart's own past: for every past bar it asks "did price reach +3 ATR before -1.5 ATR within the
next 24 bars?" and learns which mix of 22 features (momentum, trend, volatility, volume, candle
shape, where price sits in its recent range) came before a yes. It opens a long when the model's
chance for the newest closed bar crosses above 50 %. Its short twin (``strategies.mirror``) learns
the same on the upside-down chart, so it has its own model for shorts.

Honest by construction:
- Walk-forward only. Models are retrained on a fixed calendar grid (every ``retrain`` bars); each
  one sees only bars whose 24-bar outcome was already known when it was trained, and predicts
  only the bars after it. A backtest is therefore out-of-sample from the first trade to the last.
- The same code runs in backtests, Replay and paper trading, behind the same gate, picks, guards
  and live lock as every other strategy. It starts as an untested candidate like the rest.

``ml_classifier`` (``strategies.master``) is the simpler logistic-regression baseline this one has
to beat.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import numpy as np
import pandas as pd

from ..indicators import adx, atr, bollinger, directional_index, ema, macd, relative_volume, rsi
from ..models import TIMEFRAME_MS
from .families import BASE, NO_TRADE, SOURCE
from .master import TREND_RANGE, _df_key, _Master, _spec

# Trained models and finished blocks of predictions, shared by every copy of the strategy in this
# process: the paper trader asks again on every new candle, and a model trained on the same bars
# gives the same answers, so it is trained once. Keyed by the training bars' fingerprint.
_MODELS: OrderedDict = OrderedDict()
_PREDICTIONS: OrderedDict = OrderedDict()
MAX_MODELS = 400
MAX_PREDICTIONS = 20_000


def _remember(cache: OrderedDict, key, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


def feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """22 features, each scaled so it means the same on any coin and price level."""
    c, h, lo, o = df["close"], df["high"], df["low"], df["open"]
    a = atr(df)
    b = bollinger(c)
    di = directional_index(df)
    rng = (h - lo).replace(0, np.nan)
    hi20, lo20 = h.rolling(20, min_periods=20).max(), lo.rolling(20, min_periods=20).min()
    return pd.DataFrame({
        "rsi14": (rsi(c) - 50) / 50,
        "rsi7": (rsi(c, 7) - 50) / 50,
        "macd_hist": macd(c)["hist"] / a,
        "adx": adx(df) / 100,
        "di_spread": (di["plus_di"] - di["minus_di"]) / 100,
        "pct_b": (c - b["lower"]) / (b["upper"] - b["lower"]).replace(0, np.nan),
        "bb_width": (b["upper"] - b["lower"]) / c,
        "ema20_dist": (c - ema(c, 20)) / a,
        "ema50_dist": (c - ema(c, 50)) / a,
        "ema100_dist": (c - ema(c, 100)) / a,
        "ret1": c.pct_change(1),
        "ret3": c.pct_change(3),
        "ret6": c.pct_change(6),
        "ret12": c.pct_change(12),
        "ret24": c.pct_change(24),
        "atr_pct": a / c,
        "atr_ratio": a / atr(df, 50),
        "rel_vol": relative_volume(df, 20),
        "range_pos": (c - lo20) / (hi20 - lo20).replace(0, np.nan),
        "body": (c - o) / rng,
        "upper_wick": (h - np.maximum(c, o)) / rng,
        "lower_wick": (np.minimum(c, o) - lo) / rng,
    }, index=df.index).replace([np.inf, -np.inf], np.nan)


def barrier_labels(df: pd.DataFrame, horizon: int, up_atr: float, down_atr: float) -> np.ndarray:
    """1 when +``up_atr`` ATR is reached before -``down_atr`` ATR within ``horizon`` bars, else 0;
    NaN for the last ``horizon`` bars (outcome not known yet). A bar touching both counts as a loss."""
    hi, lo, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    a = atr(df).to_numpy(float)
    n = len(df)
    out = np.full(n, np.nan)
    for t in range(n - horizon):
        if not np.isfinite(a[t]):
            continue
        up, dn = c[t] + up_atr * a[t], c[t] - down_atr * a[t]
        res = 0.0
        for j in range(t + 1, t + horizon + 1):
            if lo[j] <= dn:
                break
            if hi[j] >= up:
                res = 1.0
                break
        out[t] = res
    return out


class MlBoosted(_Master):
    spec = _spec(
        name="ml_boosted",
        source=SOURCE + ": machine learning, gradient-boosted trees that learn their own entries",
        timeframes=("1h", "4h"),
        regime="Any orderly market (not high-volatility); the model itself learns which conditions pay.",
        setup="A gradient-boosted tree model (HistGradientBoosting), retrained every 250 bars on up to "
        "the last 1,500 bars of this chart, estimates the chance that price reaches +3 x ATR before "
        "-1.5 x ATR within 24 bars. Features (22): RSI 7/14, MACD histogram, ADX, DI spread, Bollinger "
        "%B and width, distance from EMA 20/50/100, 1/3/6/12/24-bar returns, ATR % and ATR 14/50 ratio, "
        "relative volume, position in the 20-bar range, candle body and wicks. Setup: that chance "
        "crosses above 50 %.",
        entry="Market order at the next bar's open.",
        stop="Entry - 1.5 x ATR(14) (the same barrier the model was trained on).",
        target="2R (+3 x ATR); time stop after 24 bars.",
        filter="No prediction until 400 labelled bars exist; a bar is only used for training once its "
        "24-bar outcome is known, and each model only predicts bars after it was trained (walk-forward, "
        "no leakage). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The model, not a hand-written rule, decides when to enter; stop, target and sizing "
                   "stay fixed so risk is known before the trade.",
                   "Retrained on a fixed calendar grid so live trading and backtests use the same models.",
                   "It has to beat ml_classifier (logistic regression) and the rule strategies in the "
                   "same backtests to be kept."),
    )
    eligible_regimes = TREND_RANGE
    reason = "ML model chance of +2R crossed 50%"
    # the paper trader loads this many bars (and starts them on the retrain grid) so the newest
    # model always has its full training window
    history_bars = 1500 + 24 + 2 * 250

    @classmethod
    def default_params(cls):
        return {**BASE, "horizon": 24, "up_atr": 3.0, "down_atr": 1.5, "min_train": 400,
                "train_bars": 1500, "retrain": 250, "threshold": 0.5, "target_r": 2.0}

    @property
    def history_align(self) -> int:
        return int(self.params["retrain"])

    def _model(self, X: np.ndarray, y: np.ndarray, key):
        model = _MODELS.get(key)
        if model is None:
            from sklearn.ensemble import HistGradientBoostingClassifier

            model = HistGradientBoostingClassifier(
                max_iter=120, learning_rate=0.05, max_depth=3, min_samples_leaf=30,
                l2_regularization=1.0, early_stopping=False, random_state=0)
            model.fit(X, y)
            _remember(_MODELS, key, model, MAX_MODELS)
        else:
            _MODELS.move_to_end(key)
        return model

    def probabilities(self, df: pd.DataFrame, timeframe: str) -> pd.Series:
        """Out-of-sample chance for every bar (NaN where no model was trained yet)."""
        p = self.params
        n, h = len(df), int(p["horizon"])
        prob = np.full(n, np.nan)
        if n < p["min_train"] + h + 2:
            return pd.Series(prob, index=df.index)
        X = feature_frame(df)
        ok = X.notna().all(axis=1).to_numpy()
        Xv = X.to_numpy(float)
        yv = barrier_labels(df, h, p["up_atr"], p["down_atr"])
        ts = df["ts"].to_numpy(np.int64)
        # a new model at every bar whose time is on the retrain grid (or the first bar after a gap)
        block_id = ts // (int(p["retrain"]) * TIMEFRAME_MS[timeframe])
        starts = np.flatnonzero(np.r_[True, block_id[1:] != block_id[:-1]])
        ends = np.r_[starts[1:], n]
        for t0, t1 in zip(starts, ends):
            rows = np.arange(max(0, t0 - h - int(p["train_bars"])), max(0, t0 - h))
            rows = rows[ok[rows] & np.isfinite(yv[rows])]
            if len(rows) < p["min_train"] or len(set(yv[rows])) < 2:
                continue
            fp = hashlib.blake2b(Xv[rows].tobytes() + yv[rows].tobytes(), digest_size=16).hexdigest()
            key = (fp, tuple(sorted((k, v) for k, v in p.items() if not isinstance(v, (list, dict)))))
            block = np.arange(t0, t1)
            block = block[ok[block]]
            if not len(block):
                continue
            done = t1 < n  # the block is complete: its answers never change again
            pkey = (key, int(ts[t0]), int(ts[t1 - 1]))
            got = _PREDICTIONS.get(pkey) if done else None
            if got is None:
                got = self._model(Xv[rows], yv[rows], key).predict_proba(Xv[block])[:, 1]
                if done:
                    _remember(_PREDICTIONS, pkey, got, MAX_PREDICTIONS)
            prob[block] = got
        return pd.Series(prob, index=df.index)

    def setup(self, df, timeframe):
        key = (_df_key(df), timeframe)
        if getattr(self, "_memo", (None,))[0] != key:
            self._memo = (key, self.probabilities(df, timeframe))
        pr = self._memo[1]
        th = self.params["threshold"]
        return (pr >= th) & (pr.shift(1) < th)

    def stop_series(self, df):
        return df["close"] - self.params["down_atr"] * atr(df)

    def exit_key(self):
        return f"time_stop={self.params['horizon']}"

    def features(self, df):
        return {"ml_chance": self._memo[1]} if getattr(self, "_memo", None) else {}


ML_STRATEGIES = (MlBoosted,)
