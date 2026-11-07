"""The paper's best model, a ResNet-LSTM, on CUSUM bars (Grądzki, Wójcik & Lessmann, 2025).

``ml_cusum_resnet_lstm`` keeps everything of ``ml_cusum_barrier`` (2 % CUSUM events, the ±5 %
triple barrier with its 3-day time barrier, the 70 % threshold, stop, target, walk-forward
retraining) and changes only the model, so the two can be compared on the same trades:

- The candles between two CUSUM events are joined into one **CUSUM bar** (open of the first candle,
  highest high, lowest low, close at the event, summed volume), as the paper samples its data.
- The paper's features are computed on those bars, and the network reads the last ``window`` bars
  (32; the paper fed 96, which would leave too few training samples on 4h candles).
- The network is the paper's Fig. 1: batch normalisation and three 1-D convolutions with ReLU,
  dropout, a skip connection around the last convolution, then an LSTM and a dense layer. The
  paper tuned its sizes with Hyperband; fixed sizes are used here so the test isn't fitted.
- Trained with Adam and early stopping on the newest 15 % of the training events (time order kept),
  three times with different seeds; the three chances are averaged (the paper averaged three runs).
- Retrained every 540 candles (90 days on 4h: quarterly, as in the paper) on the events of the last
  720 days, only on events whose outcome was already known (walk-forward).

PyTorch is needed (the server image installs its CPU-only build). Without it the strategy gives
no signals and logs why once.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .families import NO_TRADE
from .master import _spec
from .ml import (_MODELS, MAX_MODELS, SOURCE, MlCusumBarrier, _remember, cusum_events, gradzki_features)

log = logging.getLogger(__name__)
_WARNED = []


def torch_available() -> bool:
    import importlib.util

    if importlib.util.find_spec("torch") is None:  # not installed: the strategy just stays quiet
        if not _WARNED:
            _WARNED.append(True)
            log.warning("ml_cusum_resnet_lstm needs PyTorch, which is not installed: no signals")
        return False
    return True


def cusum_bars(df: pd.DataFrame, events: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    """One bar per CUSUM event from the candles since the previous event. Returns the bars and,
    for each bar, the index of its event candle in ``df``."""
    idx = np.flatnonzero(events)
    if not len(idx):
        return df.iloc[:0][["ts", "open", "high", "low", "close", "volume"]], idx
    group = np.full(len(df), -1)
    starts = np.r_[0, idx[:-1] + 1]
    for g, (a, b) in enumerate(zip(starts, idx)):
        group[a:b + 1] = g
    use = group >= 0
    d = df.loc[use, ["ts", "open", "high", "low", "close", "volume"]].assign(g=group[use])
    bars = d.groupby("g").agg(ts=("ts", "last"), open=("open", "first"), high=("high", "max"),
                              low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    return bars.reset_index(drop=True), idx


def _network(n_features: int, conv: int, kernel: int, hidden: int, dropout: float):
    import torch
    from torch import nn

    class ResNetLSTM(nn.Module):
        """Fig. 1 of the paper: three convolutions (the last with a skip connection), then an LSTM."""

        def __init__(self):
            super().__init__()
            pad = kernel // 2
            self.bn0 = nn.BatchNorm1d(n_features)
            self.c1 = nn.Conv1d(n_features, conv, kernel, padding=pad)
            self.bn1 = nn.BatchNorm1d(conv)
            self.c2 = nn.Conv1d(conv, conv, kernel, padding=pad)
            self.bn2 = nn.BatchNorm1d(conv)
            self.c3 = nn.Conv1d(conv, conv, kernel, padding=pad)
            self.drop = nn.Dropout(dropout)
            self.lstm = nn.LSTM(conv, hidden, batch_first=True)
            self.out = nn.Linear(hidden, 1)

        def forward(self, x):  # x: (batch, time, features)
            z = self.bn0(x.transpose(1, 2))
            z = torch.relu(self.c1(z))
            z2 = torch.relu(self.c2(self.bn1(z)))
            z = torch.relu(self.c3(self.bn2(z2))) + z2  # the skip connection around the last convolution
            z = self.drop(z).transpose(1, 2)
            _, (h, _) = self.lstm(z)
            return self.out(h[-1]).squeeze(-1)

    return ResNetLSTM()


class _DeepModel:
    """Three ResNet-LSTMs (different seeds) whose chances are averaged."""

    def __init__(self, params: dict):
        self.p = params

    def fit(self, X: np.ndarray, y: np.ndarray):
        import torch

        p = self.p
        self.mean = np.nanmean(X.reshape(-1, X.shape[-1]), axis=0)
        self.sd = np.nanstd(X.reshape(-1, X.shape[-1]), axis=0)
        self.sd[~np.isfinite(self.sd) | (self.sd == 0)] = 1.0
        self.mean[~np.isfinite(self.mean)] = 0.0
        Xs = torch.tensor(self._scale(X), dtype=torch.float32)
        ys = torch.tensor(y, dtype=torch.float32)
        n_val = max(1, int(len(y) * p["val_share"]))
        tr, va = slice(0, len(y) - n_val), slice(len(y) - n_val, len(y))
        pos = float(ys[tr].mean()) if len(ys[tr]) else 0.5
        weight = torch.tensor((1 - pos) / pos if 0 < pos < 1 else 1.0)  # balance wins and losses
        threads = torch.get_num_threads()
        torch.set_num_threads(1)  # small nets: one core each, the server keeps the rest
        try:
            self.nets = [self._train(Xs, ys, tr, va, weight, seed) for seed in range(int(p["seeds"]))]
        finally:
            torch.set_num_threads(threads)
        return self

    def _train(self, Xs, ys, tr, va, weight, seed):
        import torch

        p = self.p
        torch.manual_seed(seed)
        net = _network(Xs.shape[-1], int(p["conv"]), int(p["kernel"]), int(p["hidden"]), p["dropout"])
        opt = torch.optim.Adam(net.parameters(), lr=p["lr"], weight_decay=p["weight_decay"])
        loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=weight)
        gen = torch.Generator().manual_seed(seed)
        best, best_state, waited = float("inf"), None, 0
        Xt, yt, Xv, yv = Xs[tr], ys[tr], Xs[va], ys[va]
        for _epoch in range(int(p["epochs"])):
            net.train()
            order = torch.randperm(len(yt), generator=gen)
            for i in range(0, len(order), int(p["batch"])):
                b = order[i:i + int(p["batch"])]
                if len(b) < 2:  # batch normalisation needs two samples
                    continue
                opt.zero_grad()
                loss_fn(net(Xt[b]), yt[b]).backward()
                opt.step()
            net.eval()
            with torch.no_grad():
                v = float(loss_fn(net(Xv), yv))
            if v < best - 1e-4:
                best, waited = v, 0
                best_state = {k: t.clone() for k, t in net.state_dict().items()}
            else:
                waited += 1
                if waited >= int(p["patience"]):
                    break
        if best_state is not None:
            net.load_state_dict(best_state)
        net.eval()
        return net

    def _scale(self, X):
        Z = (X - self.mean) / self.sd
        return np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        import torch

        with torch.no_grad():
            Xs = torch.tensor(self._scale(X), dtype=torch.float32)
            p = np.mean([torch.sigmoid(n(Xs)).numpy() for n in self.nets], axis=0)
        return np.c_[1 - p, p]


class MlCusumResnetLstm(MlCusumBarrier):
    spec = _spec(
        name="ml_cusum_resnet_lstm",
        source=MlCusumBarrier.spec.source + "; the paper's best model, ResNet-LSTM",
        timeframes=("4h",),
        regime=MlCusumBarrier.spec.regime,
        setup="The same CUSUM events and triple barrier as ml_cusum_barrier, with the paper's best model: "
        "the candles between two events are joined into one CUSUM bar, the paper's features are worked "
        "out on those bars, and a ResNet-LSTM (three 1-D convolutions with a skip connection, then an "
        "LSTM) reads the last 32 bars. Three networks trained with different seeds are averaged. Setup: "
        "an event candle whose chance of +5 % before -5 % is 70 % or more.",
        entry="Market order at the next bar's open.",
        stop=MlCusumBarrier.spec.stop,
        target=MlCusumBarrier.spec.target,
        filter="Retrained every 90 days (quarterly, as in the paper) on the events of the last 720 days, only "
        "on events whose 3-day outcome was already known, with early stopping on the newest 15 % of them; each "
        "model predicts only later events (walk-forward, no leakage). No prediction until 300 labelled events "
        "exist. Needs PyTorch. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Only the model differs from ml_cusum_barrier (same events, labels, stop, target and "
                   "threshold), so each engine's strategy test shows whether the deep network helps.",
                   "4h only: 720 days of 1h candles would make every chart in paper trading much longer.",
                   "32 bars of history instead of the paper's 96, and fixed network sizes instead of the "
                   "paper's Hyperband search, so there are enough training samples and the test isn't fitted.",
                   "The paper's results were on ETH (best) and BTC (weaker) in 2022-2023; nothing here "
                   "promises a profit. It passes the same gate as every other strategy, per engine."),
    )
    reason = "CUSUM event: ResNet-LSTM chance of +5 % before -5 % at 70 % or more"
    history_bars = 4320 + 18 + 2 * 540

    @classmethod
    def default_params(cls):
        return {**super().default_params(), "min_train": 300, "train_bars": 4320, "retrain": 540,
                "window": 32, "conv": 32, "kernel": 3, "hidden": 32, "dropout": 0.2, "lr": 3e-3,
                "weight_decay": 1e-4, "batch": 64, "epochs": 60, "patience": 8, "val_share": 0.15, "seeds": 3}

    def _features(self, df):
        n, w = len(df), int(self.params["window"])
        X = np.full((n, w, 32), np.nan)
        ok = np.zeros(n, bool)
        if not torch_available():
            return X, ok
        ev = self._events(df)
        bars, at = cusum_bars(df, ev["event"].to_numpy())
        if len(bars) < w + 50:
            return X, ok
        bev = cusum_events(bars["close"], self.params["cusum"])  # the CUSUM sums on the bars themselves
        F = gradzki_features(bars, bev).to_numpy(float)
        X = np.full((n, w, F.shape[1]), np.nan)
        from numpy.lib.stride_tricks import sliding_window_view

        seq = sliding_window_view(F, w, axis=0).transpose(0, 2, 1)  # bar k: bars k-w+1 .. k
        good = np.isfinite(seq).all(axis=(1, 2))
        rows = at[w - 1:]
        X[rows] = seq
        ok[rows] = good
        return X, ok

    def _model(self, X, y, key):
        model = _MODELS.get(key)
        if model is None:
            model = _DeepModel(self.params).fit(X, y)
            _remember(_MODELS, key, model, MAX_MODELS)
        else:
            _MODELS.move_to_end(key)
        return model

    def features(self, df):
        return {"ml_chance": self._memo[1]} if getattr(self, "_memo", None) else {}


__all__ = ["MlCusumResnetLstm", "cusum_bars", "torch_available", "SOURCE"]
