"""A machine-learning strategy that finds its own entries: no hand-written entry rule.

``ml_boosted`` trains a gradient-boosted tree model (scikit-learn's HistGradientBoosting) on the
chart's own past: for every past bar it asks "did price reach +3 ATR before -1.5 ATR within the
next 24 bars?" and learns which mix of 25 features (momentum, trend, volatility, volume, candle
shape, where price sits in its recent range) came before a yes. It opens a long when the model's
chance for the newest closed bar crosses above 70 %. Its short twin (``strategies.mirror``) learns
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

from ..indicators import adx, atr, bollinger, cci, directional_index, ema, macd, relative_volume, rsi
from ..models import TIMEFRAME_MS
from .families import BASE, NO_TRADE, SOURCE, _exit_rule
from .master import TREND_RANGE, _df_key, _Master, _spec

# Trained models and finished blocks of predictions, shared by every copy of the strategy in this
# process: the paper trader asks again on every new candle, and a model trained on the same bars
# gives the same answers, so it is trained once. Keyed by the training bars' fingerprint.
_MODELS: OrderedDict = OrderedDict()
_PREDICTIONS: OrderedDict = OrderedDict()
MAX_MODELS = 400
MAX_PREDICTIONS = 20_000
# Whole charts already worked out by ``prime`` (the strategy test): its in-sample, out-of-sample,
# walk-forward and holdout slices read their answers from here instead of retraining every model on
# each slice. Each answer is still walk-forward (it only used candles before it), so a slice gets
# the same answers it would get with more history in front of it.
_PRIMED: OrderedDict = OrderedDict()
MAX_PRIMED = 8


def _remember(cache: OrderedDict, key, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


def feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """25 features, each scaled so it means the same on any coin and price level."""
    c, h, lo, o = df["close"], df["high"], df["low"], df["open"]
    a = atr(df)
    b = bollinger(c)
    di = directional_index(df)
    rng = (h - lo).replace(0, np.nan)
    hi20, lo20 = h.rolling(20, min_periods=20).max(), lo.rolling(20, min_periods=20).min()
    return pd.DataFrame({
        "rsi14": (rsi(c) - 50) / 50,
        "rsi7": (rsi(c, 7) - 50) / 50,
        "rsi30": (rsi(c, 30) - 50) / 50,  # Asgari & Khasteh's indicator set: RSI and CCI on 14 and 30
        "cci14": cci(df, 14) / 100,
        "cci30": cci(df, 30) / 100,
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
        "-1.5 x ATR within 24 bars. Features (25): RSI 7/14/30, CCI 14/30, MACD histogram, ADX, DI spread, Bollinger "
        "%B and width, distance from EMA 20/50/100, 1/3/6/12/24-bar returns, ATR % and ATR 14/50 ratio, "
        "relative volume, position in the 20-bar range, candle body and wicks. Setup: that chance "
        "crosses above 70 % (Shawn's choice: only confident calls trade).",
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
    reason = "ML model chance of +2R crossed 70%"
    # the paper trader loads this many bars (and starts them on the retrain grid) so the newest
    # model always has its full training window
    history_bars = 1500 + 24 + 2 * 250

    @classmethod
    def default_params(cls):
        return {**BASE, "horizon": 24, "up_atr": 3.0, "down_atr": 1.5, "min_train": 400,
                "train_bars": 1500, "retrain": 250, "threshold": 0.7, "target_r": 2.0}

    def _horizon(self, timeframe: str) -> int:
        """Bars until a label's outcome is known."""
        return int(self.params["horizon"])

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

    def _features(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """(feature rows, which rows are complete)."""
        X = feature_frame(df)
        return X.to_numpy(float), X.notna().all(axis=1).to_numpy()

    def _labels(self, df: pd.DataFrame) -> np.ndarray:
        p = self.params
        return barrier_labels(df, int(p["horizon"]), p["up_atr"], p["down_atr"])

    def _primed_key(self, timeframe: str):
        return (type(self).__name__, timeframe,
                tuple(sorted((k, v) for k, v in self.params.items() if not isinstance(v, (list, dict)))))

    def prime(self, df: pd.DataFrame, timeframe: str) -> None:
        """Work out the whole chart once, so slices of it (``_from_primed``) need no new models."""
        prob = self._compute(df, timeframe).to_numpy(float)
        key = (self._primed_key(timeframe), int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]), len(df),
               float(df["close"].iloc[-1]))
        _remember(_PRIMED, key, (df["ts"].to_numpy(np.int64), df["close"].to_numpy(float), prob), MAX_PRIMED)

    def _from_primed(self, df: pd.DataFrame, timeframe: str):
        """The answers for ``df`` when it is a stretch of a primed chart (same candles), else None."""
        if not len(df):
            return None
        mine = self._primed_key(timeframe)
        ts, close = df["ts"].to_numpy(np.int64), df["close"].to_numpy(float)
        for key, (pts, pclose, prob) in reversed(_PRIMED.items()):
            if key[0] != mine or ts[0] < pts[0] or ts[-1] > pts[-1]:
                continue
            i = np.searchsorted(pts, ts)
            if (i < len(pts)).all() and np.array_equal(pts[np.minimum(i, len(pts) - 1)], ts) \
                    and np.array_equal(pclose[i], close):
                return prob[i]
        return None

    def probabilities(self, df: pd.DataFrame, timeframe: str) -> pd.Series:
        """Out-of-sample chance for every bar (NaN where no model was trained yet)."""
        self._tf = timeframe
        hit = self._from_primed(df, timeframe) if _PRIMED else None
        if hit is not None:
            return pd.Series(hit, index=df.index)
        return self._compute(df, timeframe)

    def _compute(self, df: pd.DataFrame, timeframe: str) -> pd.Series:
        p = self.params
        self._tf = timeframe
        n, h = len(df), self._horizon(timeframe)
        prob = np.full(n, np.nan)
        if n < p["min_train"] + h + 2:
            return pd.Series(prob, index=df.index)
        Xv, ok = self._features(df)
        yv = self._labels(df)
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
            key = (type(self).__name__, fp,
                   tuple(sorted((k, v) for k, v in p.items() if not isinstance(v, (list, dict)))))
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


# ---- Asgari & Khasteh (2021): k-NN, Random Forest and gradient boosting on the next 4 hours ---------


def paper_rows(df: pd.DataFrame) -> pd.DataFrame:
    """The paper's 19 values per candle (OHLCV plus its Appendix A indicators: CCI 14/30, RSI 14/30,
    DMI 14, MACD, Bollinger bands), each made price-free so one coin's model reads like another's.
    The paper scaled each column by its min and max over the whole history, which lets the test
    period leak into training; here every value is a ratio of the candle's own prices instead."""
    c, h, lo, o = df["close"], df["high"], df["low"], df["open"]
    a = atr(df).replace(0, np.nan)
    di = directional_index(df)
    m = macd(c)
    tp = (h + lo + c) / 3
    mid = tp.rolling(20, min_periods=20).mean()
    sd = tp.rolling(20, min_periods=20).std(ddof=0)
    pdi, mdi = di["plus_di"], di["minus_di"]
    return pd.DataFrame({
        "open": o / c - 1, "high": h / c - 1, "low": lo / c - 1, "ret1": c.pct_change(1),
        "volume": np.log((df["volume"] / df["volume"].rolling(60, min_periods=20).mean()).replace(0, np.nan)),
        "cci14": cci(df, 14) / 100, "cci30": cci(df, 30) / 100,
        "rsi14": (rsi(c) - 50) / 50, "rsi30": (rsi(c, 30) - 50) / 50,
        "plus_di": pdi / 100, "minus_di": mdi / 100,
        "dx": (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan),
        "macd": m["macd"] / a, "macd_signal": m["signal"] / a, "macd_hist": m["hist"] / a,
        "boll_up": (mid + 2 * sd - c) / a, "boll_down": (c - (mid - 2 * sd)) / a, "boll_mid": (c - mid) / a,
        "atr_pct": a / c,
    }, index=df.index).replace([np.inf, -np.inf], np.nan)


class MlEnsemble(MlBoosted):
    spec = _spec(
        name="ml_ensemble",
        source=SOURCE + ": machine learning, Asgari & Khasteh (2021) 'Profitable Strategy Design for "
        "Trades on Cryptocurrency Markets with Machine Learning Techniques': k-NN, Random Forest and "
        "gradient-boosted trees voting together",
        timeframes=("1h", "4h"),
        regime="Any orderly market (not high-volatility); the models learn which conditions pay.",
        setup="Three models from the paper (k-NN with 20 distance-weighted neighbours, a Random Forest "
        "and gradient-boosted trees with XGBoost's eta 0.3 / depth 6 / lambda 1, in scikit-learn) "
        "each estimate the chance that the price 4 hours later is more than 0.15 % (the round-trip fee) "
        "above the close. Each reads the last 60 candles x 19 values (OHLCV, CCI 14/30, RSI 14/30, "
        "DMI 14, MACD, Bollinger bands) = 1,140 inputs, as in the paper. Their average is the chance "
        "(the paper's future-work idea of ensembling them). Setup: that chance crosses above 70 % "
        "(Shawn's choice: only confident calls trade).",
        entry="Market order at the next bar's open.",
        stop="Entry - 2 x ATR(14): a safety stop the paper did not have.",
        target="As in the paper, it holds while the models still expect a rise and sells at the first "
        "candle where they don't; a 4R ceiling and a time stop of 12 x 4 hours as backstops.",
        filter="Retrained every 250 candles on up to the last 1,500, only on candles whose 4-hour outcome "
        "was already known, and each model predicts only later candles (walk-forward, no leakage). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The paper's best results were a profit factor of 1.12-1.63 over one 66-day test on "
                   "three pairs, with accuracy near 52 % and no stop loss; here it must pass the same "
                   "walk-forward, Monte Carlo and holdout gate as every other strategy, per engine.",
                   "The paper tuned k and the number of trees per pair; one setting is used here, so the "
                   "test is not fitted to the pairs it is judged on.",
                   "Labels need a rise bigger than the fees, as the paper says: a smaller one loses money.",
                   "60 trees and 40 boosting rounds instead of the paper's 700-1,000 trees, so a retrain takes "
                   "seconds on the server's CPU; the three models still vote."),
    )
    reason = "k-NN, Random Forest and gradient boosting expect a 4h rise above the fees"

    @classmethod
    def default_params(cls):
        return {**BASE, "hours": 4, "fee_threshold": 0.0015, "lookback": 60, "min_train": 400,
                "train_bars": 1500, "retrain": 250, "threshold": 0.7, "exit_below": 0.5, "knn_k": 20, "rf_trees": 60, "gb_rounds": 40,
                "stop_atr": 2.0, "target_r": 4.0, "max_hold": 12}

    history_bars = 1500 + 60 + 4 + 2 * 250

    def __init__(self, **params):
        super().__init__(**params)
        # the exit "the models no longer expect a rise", one per timeframe, registered up front so a
        # restarted paper trader can manage an open trade before it builds new signals
        for tf in self.spec.timeframes:
            _exit_rule(self._exit_name(tf), lambda d, tf=tf: self.probabilities(d, tf) < self.params["exit_below"])

    def _exit_name(self, timeframe: str) -> str:
        p = self.params
        return f"{self.spec.name}_down:{timeframe}:{p['exit_below']:g}:{p['hours']:g}:{p['fee_threshold']:g}"

    def _horizon(self, timeframe: str) -> int:
        return max(1, round(self.params["hours"] * 3_600_000 / TIMEFRAME_MS[timeframe]))

    def _features(self, df):
        from numpy.lib.stride_tricks import sliding_window_view

        rows = paper_rows(df).to_numpy(float)
        n, w = len(rows), int(self.params["lookback"])
        X = np.full((n, w * rows.shape[1]), np.nan)
        if n >= w:  # row t holds candles t-59 .. t, oldest first
            X[w - 1:] = sliding_window_view(rows, w, axis=0).transpose(0, 2, 1).reshape(n - w + 1, -1)
        return X, np.isfinite(X).all(axis=1)

    def _labels(self, df):
        c = df["close"].to_numpy(float)
        h = self._horizon(self._tf)
        out = np.full(len(c), np.nan)
        if len(c) > h:
            out[:-h] = (c[h:] / c[:-h] - 1 > self.params["fee_threshold"]).astype(float)
        return out

    def _model(self, X, y, key):
        model = _MODELS.get(key)
        if model is None:
            from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
            from sklearn.impute import SimpleImputer
            from sklearn.neighbors import KNeighborsClassifier
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler

            p = self.params
            model = _Vote([
                make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                              KNeighborsClassifier(n_neighbors=int(p["knn_k"]),
                                                                     weights="distance")),
                RandomForestClassifier(n_estimators=int(p["rf_trees"]), min_samples_leaf=3, max_features="sqrt",
                                       n_jobs=1, random_state=0),
                HistGradientBoostingClassifier(learning_rate=0.3, max_depth=6, l2_regularization=1.0,
                                               max_iter=int(p["gb_rounds"]), max_features=0.3,
                                               early_stopping=False, random_state=0),
            ]).fit(X, y)
            _remember(_MODELS, key, model, MAX_MODELS)
        else:
            _MODELS.move_to_end(key)
        return model

    def stop_series(self, df):
        return df["close"] - self.params["stop_atr"] * atr(df)

    def exit_key(self):
        tf = getattr(self, "_tf", None) or self.spec.timeframes[0]
        return f"{self._exit_name(tf)}|time_stop={self.params['max_hold'] * self._horizon(tf)}"

    def features(self, df):
        return {"ml_chance": self._memo[1]} if getattr(self, "_memo", None) else {}


class _Vote:
    """The three models' average chance of a rise (soft voting)."""

    def __init__(self, models):
        self.models = models

    def fit(self, X, y):
        for m in self.models:
            m.fit(X, y)
        return self

    def predict_proba(self, X):
        return np.mean([m.predict_proba(X) for m in self.models], axis=0)


# ---- the fundamental overlay: the perpetual's funding, open interest and basis as model inputs ----

FUND_COLUMNS = ("funding", "funding_chg3d", "oi_chg4h", "oi_chg1d", "premium", "premium_z7d")
FUND_TEXT = ("Fundamental overlay (6 more inputs, from Bybit's perpetual for this coin): funding rate, "
             "funding against its 3-day average, open-interest change over 4 hours and 1 day, the premium "
             "index (basis: perpetual against the index price) and how unusual it is against the last 7 "
             "days. No trade without a fresh funding rate and premium index; open interest may be missing.")


def fund_frame(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """The perpetual's funding, open interest and premium index as scaled inputs, from the columns
    ``data.context.attach_context`` adds (NaN where they are missing). Each bar only reads values
    stamped at or before its close."""
    nan = pd.Series(np.nan, index=df.index)
    f = df["funding_rate"] if "funding_rate" in df else nan
    oi = (df["open_interest"] if "open_interest" in df else nan).replace(0, np.nan)
    pr = df["premium_index"] if "premium_index" in df else nan
    per_hour = 3_600_000 / TIMEFRAME_MS[timeframe]
    day, week, h4 = max(1, round(24 * per_hour)), max(2, round(168 * per_hour)), max(1, round(4 * per_hour))
    days3 = max(1, round(72 * per_hour))
    sd = pr.rolling(week, min_periods=week // 2).std(ddof=0).replace(0, np.nan)
    return pd.DataFrame({
        "funding": f * 1000,  # +0.01 % per 8h -> 0.1
        "funding_chg3d": (f - f.rolling(days3, min_periods=1).mean()) * 1000,
        "oi_chg4h": oi / oi.shift(h4) - 1,
        "oi_chg1d": oi / oi.shift(day) - 1,
        "premium": pr * 100,
        "premium_z7d": (pr - pr.rolling(week, min_periods=week // 2).mean()) / sd,
    }, index=df.index).replace([np.inf, -np.inf], np.nan)


def _fund_needed(F: pd.DataFrame) -> np.ndarray:
    """Rows with the inputs a trade can't go without: funding and premium index (open interest may
    be missing; the trees and the filled-in k-NN handle that)."""
    return F[["funding", "funding_chg3d", "premium"]].notna().all(axis=1).to_numpy()


def _fund_spec(base, name: str, extra: str):
    from dataclasses import replace

    return replace(base, name=name, setup=base.setup + " " + FUND_TEXT,
                   filter=base.filter + " " + extra,
                   decisions=base.decisions + (
                       "A separate strategy next to the one without fundamentals, so each engine's strategy "
                       "test shows whether the extra inputs help on candles the models never saw.",))


class MlBoostedFund(MlBoosted):
    spec = _fund_spec(MlBoosted.spec, "ml_boosted_fund",
                      "It only trains on candles that had funding and premium data at the time.")
    needs_context = True
    reason = "ML model with funding / open interest / basis: chance of +2R crossed 70%"

    def _features(self, df):
        X = feature_frame(df)
        F = fund_frame(df, getattr(self, "_tf", None) or self.spec.timeframes[0])
        both = pd.concat([X, F], axis=1)
        return both.to_numpy(float), X.notna().all(axis=1).to_numpy() & _fund_needed(F)


class MlEnsembleFund(MlEnsemble):
    spec = _fund_spec(MlEnsemble.spec, "ml_ensemble_fund",
                      "The six fundamental inputs are the newest candle's, next to its 60-candle window; it "
                      "only trains on candles that had funding and premium data at the time.")
    needs_context = True
    reason = "k-NN, Random Forest and gradient boosting with funding / open interest / basis expect a 4h rise above the fees"

    def _features(self, df):
        X, ok = super()._features(df)
        F = fund_frame(df, getattr(self, "_tf", None) or self.spec.timeframes[0])
        return np.hstack([X, F.to_numpy(float)]), ok & _fund_needed(F)


ML_STRATEGIES = (MlBoosted, MlEnsemble, MlBoostedFund, MlEnsembleFund)
