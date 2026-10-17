"""Train and test the trade filter (roadmap Phase 4, steps 3-5).

The model is gradient boosting (scikit-learn's ``HistGradientBoostingClassifier``, the same
histogram method LightGBM uses, with no extra system libraries) predicting the probability that
a signal reaches 1R before its stop.

Validation is chronological and the same model that is tested is the one that ships:

    oldest 60% of signals -> fit the model
    next 20%              -> pick the probability threshold (validation)
    newest 20%            -> test: filtered vs unfiltered, never used for any choice

Training rows whose trade was still open when the validation period starts are dropped, so no
outcome from the later period leaks into the fit. On the test period both sides trade one
position per strategy and market, so a skipped signal frees the slot for the next one, exactly
as the backtest would. The model is only eligible for promotion when it beats the unfiltered
rules there (``Gate``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..backtest import metrics
from .dataset import LABEL, take_sequential
from .features import CATEGORICAL, NUMERIC

MAX_CATEGORIES = 250  # the model's limit per categorical feature (255 bins)


@dataclass(frozen=True)
class TrainConfig:
    train_frac: float = 0.6
    val_frac: float = 0.2
    min_rows: int = 200
    keep_grid: tuple[float, ...] = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)
    min_size: float = 0.5  # smallest position, as a fraction of the strategy's normal risk
    max_iter: int = 200
    learning_rate: float = 0.05
    max_leaf_nodes: int = 15
    min_samples_leaf: int = 20
    l2_regularization: float = 1.0
    seed: int = 0


@dataclass(frozen=True)
class Gate:
    """When a filter counts as better than taking every signal, on the test period."""

    min_test_trades: int = 30  # unfiltered trades needed for a meaningful comparison
    min_filtered_trades: int = 15
    min_gain_r: float = 0.02  # expectancy improvement per trade, after costs
    max_dd_ratio: float = 0.9  # or: drawdown at most 90% of unfiltered, expectancy no worse

    def check(self, base: dict, filt: dict) -> list[str]:
        why = []
        if base["trades"] < self.min_test_trades:
            why.append(f"only {base['trades']} unfiltered test trades (need {self.min_test_trades})")
        if filt["trades"] < self.min_filtered_trades:
            why.append(f"only {filt['trades']} filtered test trades (need {self.min_filtered_trades})")
        gain = filt["expectancy_r"] - base["expectancy_r"]
        better_exp = gain >= self.min_gain_r
        better_dd = (filt["max_drawdown_r"] <= base["max_drawdown_r"] * self.max_dd_ratio
                     and gain >= 0)
        if not (better_exp or better_dd):
            why.append(f"does not beat the unfiltered rules on the test period: expectancy "
                       f"{filt['expectancy_r']:+.3f}R vs {base['expectancy_r']:+.3f}R, drawdown "
                       f"{filt['max_drawdown_r']:.1f}R vs {base['max_drawdown_r']:.1f}R")
        if filt["trades"] and filt["expectancy_r"] <= 0:
            why.append(f"filtered expectancy {filt['expectancy_r']:+.3f}R is not positive")
        return why


@dataclass
class FilterModel:
    """A fitted filter: the estimator plus everything needed to turn a feature row into a
    decision. Pickled whole by the model registry."""

    estimator: object
    categories: dict[str, list[str]]
    numeric: list[str]
    threshold: float = 0.0
    full_size_p: float = 1.0
    min_size: float = 0.5
    extra: dict = field(default_factory=dict)

    def matrix(self, frame: pd.DataFrame) -> np.ndarray:
        cols = []
        for c in CATEGORICAL:
            lookup = {v: i for i, v in enumerate(self.categories[c])}
            cols.append(frame[c].astype(str).map(lookup).astype(float).to_numpy())  # unseen -> NaN
        for c in self.numeric:
            cols.append(pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float)
                        if c in frame else np.full(len(frame), np.nan))
        return np.column_stack(cols) if cols else np.empty((len(frame), 0))

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if not len(frame):
            return np.empty(0)
        return self.estimator.predict_proba(self.matrix(frame))[:, 1]

    def sizes(self, p) -> np.ndarray:
        """Position size per probability: 0 below the threshold, then from ``min_size`` up to
        1.0 (the strategy's normal risk, which is already capped) at ``full_size_p``."""
        p = np.asarray(p, float)
        span = self.full_size_p - self.threshold
        scale = np.clip((p - self.threshold) / span, 0, 1) if span > 0 else np.ones_like(p)
        return np.where(p >= self.threshold, self.min_size + (1 - self.min_size) * scale, 0.0)

    def decide(self, p: float) -> tuple[bool, float]:
        size = float(self.sizes([p])[0])
        return size > 0, size


def _categories(frame: pd.DataFrame) -> dict[str, list[str]]:
    out = {}
    for c in CATEGORICAL:
        counts = frame[c].astype(str).value_counts()
        out[c] = sorted(counts.index[:MAX_CATEGORIES])
    return out


def fit_model(train: pd.DataFrame, cfg: TrainConfig) -> FilterModel:
    from sklearn.ensemble import HistGradientBoostingClassifier

    # features with no values at all (funding / open interest until those are stored) are left
    # out of this model; a later retrain picks them up once data exists
    numeric = [c for c in NUMERIC if c in train and pd.to_numeric(train[c], errors="coerce").notna().any()]
    model = FilterModel(estimator=None, categories=_categories(train), numeric=numeric,
                        min_size=cfg.min_size)
    est = HistGradientBoostingClassifier(
        max_iter=cfg.max_iter, learning_rate=cfg.learning_rate, max_leaf_nodes=cfg.max_leaf_nodes,
        min_samples_leaf=cfg.min_samples_leaf, l2_regularization=cfg.l2_regularization,
        categorical_features=[True] * len(CATEGORICAL) + [False] * len(model.numeric),
        early_stopping=False, random_state=cfg.seed)
    est.fit(model.matrix(train), train[LABEL].astype(int).to_numpy())
    model.estimator = est
    return model


def split(frame: pd.DataFrame, cfg: TrainConfig):
    """Chronological train / validation / test split by signal time, with the purge."""
    frame = frame.sort_values("signal_ts").reset_index(drop=True)
    ts = frame["signal_ts"].to_numpy()
    t1 = ts[int(len(ts) * cfg.train_frac)]
    t2 = ts[min(len(ts) - 1, int(len(ts) * (cfg.train_frac + cfg.val_frac)))]
    train = frame[(ts < t1) & (frame["exit_ts"].to_numpy() < t1)]
    val = frame[(ts >= t1) & (ts < t2) & (frame["exit_ts"].to_numpy() < t2)]
    test = frame[ts >= t2]
    return train, val, test


def outcome(rows: pd.DataFrame, mask=None, size=None) -> dict:
    """Metrics of what a one-position-per-market trader would get from ``rows``."""
    taken = take_sequential(rows, mask)
    r = rows["r"].to_numpy()[taken]
    if size is not None:
        r = r * np.asarray(size)[taken]
    m = metrics([SimpleNamespace(r=float(x), cost_r=0.0) for x in r])
    m.pop("cost_r_per_trade", None)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}


def choose_threshold(model: FilterModel, val: pd.DataFrame, cfg: TrainConfig,
                     min_trades: int) -> dict:
    p = model.predict(val)
    best = None
    for keep in cfg.keep_grid:
        thr = 0.0 if keep >= 1 else float(np.quantile(p, 1 - keep))
        m = outcome(val, p >= thr)
        if m["trades"] < min_trades:
            continue
        if best is None or m["expectancy_r"] > best["validation"]["expectancy_r"] + 1e-9:
            best = {"keep": keep, "threshold": thr, "validation": m}
    if best is None:
        best = {"keep": 1.0, "threshold": 0.0, "validation": outcome(val)}
    above = p[p >= best["threshold"]]
    best["full_size_p"] = float(np.quantile(above, 0.9)) if len(above) else 1.0
    return best


def _scores(y: np.ndarray, p: np.ndarray) -> dict:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    out = {"rows": int(len(y)), "base_rate": round(float(y.mean()), 4) if len(y) else None}
    if len(set(y)) == 2:
        out["auc"] = round(float(roc_auc_score(y, p)), 4)
        out["brier"] = round(float(brier_score_loss(y, p)), 4)
    return out


def _importance(model: FilterModel, test: pd.DataFrame, seed: int) -> list[tuple[str, float]]:
    from sklearn.inspection import permutation_importance

    y = test[LABEL].astype(int).to_numpy()
    if len(set(y)) < 2 or len(test) < 50:
        return []
    res = permutation_importance(model.estimator, model.matrix(test), y, scoring="roc_auc",
                                 n_repeats=3, random_state=seed)
    names = CATEGORICAL + model.numeric
    ranked = sorted(zip(names, res.importances_mean), key=lambda x: -x[1])
    return [(n, round(float(v), 4)) for n, v in ranked[:10]]


def _period(rows: pd.DataFrame) -> dict:
    if not len(rows):
        return {"rows": 0}
    ts = pd.to_datetime(rows["signal_ts"], unit="ms", utc=True)
    return {"rows": int(len(rows)), "from": ts.min().isoformat(), "to": ts.max().isoformat()}


def train(frame: pd.DataFrame, cfg: TrainConfig | None = None, gate: Gate | None = None,
          current: FilterModel | None = None) -> tuple[FilterModel, dict]:
    """Fit, choose the threshold, and test against the unfiltered rules (and ``current``, the
    active model, if any). Returns the model and its report."""
    cfg, gate = cfg or TrainConfig(), gate or Gate()
    if len(frame) < cfg.min_rows:
        raise ValueError(f"only {len(frame)} signals in the training set (need {cfg.min_rows}); "
                         "download more history or add strategies")
    tr, val, test = split(frame, cfg)
    if tr[LABEL].nunique() < 2:
        raise ValueError("the training period has only one outcome (all wins or all losses)")
    model = fit_model(tr, cfg)
    choice = choose_threshold(model, val, cfg, gate.min_filtered_trades)
    model.threshold, model.full_size_p = choice["threshold"], choice["full_size_p"]

    p = model.predict(test)
    base = outcome(test)
    filt = outcome(test, p >= model.threshold)
    sized = outcome(test, p >= model.threshold, model.sizes(p))
    why = gate.check(base, filt)
    report = {
        "config": asdict(cfg), "gate": asdict(gate),
        "periods": {"train": _period(tr), "validation": _period(val), "test": _period(test)},
        "threshold": round(model.threshold, 4), "keep_fraction": choice["keep"],
        "full_size_p": round(model.full_size_p, 4),
        "validation": choice["validation"],
        "test": {"unfiltered": base, "filtered": filt, "filtered_sized": sized},
        "probability": _scores(test[LABEL].astype(int).to_numpy(), p),
        "importance": _importance(model, test, cfg.seed),
        "per_strategy": _per_strategy(test, p >= model.threshold),
        "passed": not why, "failed": why,
    }
    if current is not None:
        pc = current.predict(test)
        report["test"]["active_model"] = outcome(test, pc >= current.threshold)
        report["test"]["active_model_sized"] = outcome(test, pc >= current.threshold,
                                                       current.sizes(pc))
    model.extra = {"features": CATEGORICAL + model.numeric}
    return model, report


def _per_strategy(test: pd.DataFrame, mask: np.ndarray, top: int = 15) -> list[dict]:
    out = []
    counts = test["strategy"].value_counts()
    for sid in counts.index[:top]:
        sel = (test["strategy"] == sid).to_numpy()
        rows = test[sel]
        b, f = outcome(rows), outcome(rows, mask[sel])
        out.append({"strategy": sid, "unfiltered_trades": b["trades"],
                    "unfiltered_exp": b["expectancy_r"], "filtered_trades": f["trades"],
                    "filtered_exp": f["expectancy_r"]})
    return out


def report_markdown(version: str, report: dict, active: str | None = None) -> str:
    """The side-by-side report a person reads before promoting a model."""
    t = report["test"]
    cols = [("Unfiltered rules", t["unfiltered"]), (f"New model {version}", t["filtered"]),
            ("New model, sized", t["filtered_sized"])]
    if "active_model" in t:
        cols += [(f"Active model {active}", t["active_model"]),
                 ("Active model, sized", t["active_model_sized"])]
    lines = [f"# Trade filter {version}", "",
             f"**Verdict:** {'beats the unfiltered rules on the test period' if report['passed'] else 'not eligible'}"]
    for why in report["failed"]:
        lines.append(f"- {why}")
    p = report["periods"]
    lines += ["", "## Periods (chronological, test never used for any choice)", "",
              "| Part | Signals | From | To |", "|---|---|---|---|"]
    for name in ("train", "validation", "test"):
        q = p[name]
        lines.append(f"| {name} | {q['rows']} | {q.get('from', '')[:10]} | {q.get('to', '')[:10]} |")
    lines += ["", "## Test period, side by side", "",
              "| | " + " | ".join(c for c, _ in cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for key, label, fmt in [("trades", "Trades", "{}"), ("win_rate", "Win rate", "{:.0%}"),
                            ("expectancy_r", "Expectancy (R/trade)", "{:+.3f}"),
                            ("profit_factor", "Profit factor", "{:.2f}"),
                            ("max_drawdown_r", "Max drawdown (R)", "{:.1f}"),
                            ("total_r", "Total R", "{:+.1f}")]:
        lines.append(f"| {label} | " + " | ".join(fmt.format(m[key]) for _, m in cols) + " |")
    pr = report["probability"]
    lines += ["", f"Threshold {report['threshold']:.3f} (keeps about {report['keep_fraction']:.0%} "
              f"of signals on validation); full size from p = {report['full_size_p']:.3f}.",
              f"Test signals {pr['rows']}, base rate of reaching 1R {pr.get('base_rate')}, "
              f"AUC {pr.get('auc', 'n/a')}, Brier {pr.get('brier', 'n/a')}."]
    if report["importance"]:
        lines += ["", "## Features that matter most (permutation importance on test)", ""]
        lines += [f"- {n}: {v:+.4f}" for n, v in report["importance"]]
    if report["per_strategy"]:
        lines += ["", "## Per strategy (test period)", "",
                  "| Strategy | Unfiltered trades | Unfiltered R | Filtered trades | Filtered R |",
                  "|---|---|---|---|---|"]
        for s in report["per_strategy"]:
            lines.append(f"| {s['strategy']} | {s['unfiltered_trades']} | {s['unfiltered_exp']:+.3f} | "
                         f"{s['filtered_trades']} | {s['filtered_exp']:+.3f} |")
    if "active_model" in t:
        lines += ["", "Note: the active model may have been trained on part of this test period, "
                  "which flatters its column."]
    return "\n".join(lines) + "\n"
