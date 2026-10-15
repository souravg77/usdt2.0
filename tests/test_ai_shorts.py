"""The trade filter judges shorts as the long on the flipped chart, and learns from them the same way."""

import pandas as pd
import pytest

from aitrader.ai.dataset import signal_rows
from aitrader.ai.features import as_long, market_features
from aitrader.ai.live import LiveFilter
from aitrader.models import Side
from aitrader.strategies import STRATEGIES
from aitrader.strategies.base import Signal
from aitrader.strategies.mirror import ShortMirror, flip

from test_strategies import synthetic


def _short():
    return Signal(ts=0, strategy="s", exchange="bybit", symbol="BTC/USDT", timeframe="1h", side=Side.SELL,
                  entry=100.0, stop=104.0, target=92.0)


def test_a_short_becomes_the_long_on_the_flipped_chart():
    df = synthetic(n=50)
    sig, fdf = as_long(_short(), df)
    assert sig.side is Side.BUY and sig.entry == pytest.approx(0.01) and sig.stop < sig.entry < sig.target
    assert fdf["high"].iloc[0] == pytest.approx(1 / df["low"].iloc[0])
    long = Signal(**{**_short().__dict__, "side": Side.BUY, "stop": 96.0, "target": 108.0})
    assert as_long(long, df) == (long, df)


def test_the_live_filter_judges_a_short():
    df = synthetic(n=400)
    seen = []

    class Model:
        def predict(self, rows):
            seen.append(rows.iloc[0])
            return [0.3]

        def decide(self, p):
            return p >= 0.5, 1.0

    f = LiveFilter("/nonexistent")
    f._refresh = lambda: None
    f.model, f.version = Model(), "v1"
    out = f.check(Signal(**{**_short().__dict__, "entry": float(df["close"].iloc[-1]),
                            "stop": float(df["close"].iloc[-1]) * 1.03, "target": None}), "short-x", df, [])
    assert out == {"take": False, "size": 1.0, "prob": 0.3, "version": "v1"}
    flipped = market_features(flip(df)).iloc[-1]
    assert seen[0]["rsi"] == pytest.approx(flipped["rsi"]) and seen[0]["stop_atr"] > 0


def test_short_twins_add_rows_to_the_training_set():
    df = synthetic(n=1500)
    base = STRATEGIES["trend_pullback"]()
    rows = signal_rows(ShortMirror(base), "short-builtin-trend_pullback", df, "bybit", "BTC/USDT", "1h")
    shorts = ShortMirror(base).generate(df, "bybit", "BTC/USDT", "1h")
    assert rows and len(rows) <= len(shorts)
    assert {r["strategy"] for r in rows} == {"short-builtin-trend_pullback"}
    assert all(r["stop_atr"] > 0 and r["hit_1r"] in (0, 1) for r in rows)
    assert pd.DataFrame(rows)["signal_ts"].isin([s.ts for s in shorts]).all()


def test_a_closed_paper_short_becomes_a_row(tmp_path):
    import json

    from aitrader.ai.dataset import paper_rows
    from aitrader.data.store import CandleStore

    from test_learn import H, fill_store

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=900)
    fill_store(store, df)
    i = 700
    close = float(df["close"].iloc[i])
    trade = {"key": "short-x|bybit|BTC/USDT|1h", "strategy": "s", "side": "sell", "signal_ts": int(df.ts.iloc[i]) + H,
             "entry_ts": int(df.ts.iloc[i + 1]), "exit_ts": int(df.ts.iloc[i + 5]), "exit_reason": "stop",
             "entry": float(df["open"].iloc[i + 1]), "stop": close * 1.02, "target": close * 0.96, "r": -1.0}
    (tmp_path / "s.json").write_text(json.dumps({"trades": [trade]}))
    rows = paper_rows(store, str(tmp_path / "s.json"))
    store.close()
    assert len(rows) == 1 and rows[0]["stop_atr"] > 0 and 1.8 < rows[0]["target_r"] < 2.3  # 2R, measured on the flipped chart
