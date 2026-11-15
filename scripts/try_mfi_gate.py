"""Test-only: Shawn's Volume (20 SMA) + MFI (14) confirmation layer in front of every strategy.

Runs your current strategies twice through the platform's own backtest gate on this server's
candles: once as they are (layer OFF) and once where a signal only becomes a trade when it clears
the layer (layer ON). Nothing is added to the registry and nothing trades; it prints a before/after
report and saves it.

    sudo docker compose run --rm -v ~/try_mfi_gate.py:/app/try_mfi_gate.py \
        --entrypoint python collector /app/try_mfi_gate.py

Options: --market futures|spot (default futures), --need 3 (checks out of 4 that must pass),
--max-strategies 40, --max-pairs 20, --no-ml (leave out the slower ML strategies).

The layer, on the signal's own candle (the candle that just closed), written from Shawn's
Go / No-Go checklist (2026-11-15). A trade opens only when at least 3 of the 4 checks pass:

  1. Chart level   long: the candle's low is within 0.5 ATR of a support zone, or it closed above a
                   resistance zone (a breakout). short: at resistance, or closed below support.
                   Zones = swing highs/lows of the last ~40 swings (5 bars each side) on this chart.
  2. Volume        the candle's volume >= 1.5 x its 20-bar SMA, and the candle is green for a long
                   (red for a short).
  3. MFI (14)      long: MFI was below 20 within the last 5 candles and is now back above 20, and
                   below 50. short: was above 80 within the last 5 and is now back below 80, and
                   above 50.
  4. Candle close  long: closes in the top third of its range with a body >= 50% of the range.
                   short: bottom third, same body.

Decisions (the checklist doesn't say): levels come from the strategy's own chart, not a separate
4h/1d chart; "bouncing out of <20" means a dip below 20 in the last 5 candles.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

import aitrader.learn.lab as lab
from aitrader.backtest import BacktestConfig
from aitrader.cli import EXCHANGES, _engine_market
from aitrader.config import Settings
from aitrader.indicators import atr, mfi
from aitrader.learn import Registry
from aitrader.learn.picks import learns_for
from aitrader.models import TIMEFRAME_MS, Side

PIVOT, LEVELS = 5, 40
CHECKS = ("level", "volume", "mfi", "candle")


def levels(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Nearest support below and resistance above each close, from swing points known by then."""
    h, lo, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(df)
    sup, res = np.full(n, np.nan), np.full(n, np.nan)
    lv: list[float] = []
    for t in range(n):
        p = t - PIVOT
        if p >= PIVOT:
            w = slice(p - PIVOT, p + PIVOT + 1)
            if h[p] >= h[w].max():
                lv.append(h[p])
            if lo[p] <= lo[w].min():
                lv.append(lo[p])
            lv = lv[-LEVELS:]
        if lv:
            a = np.asarray(lv)
            b, u = a[a < c[t]], a[a > c[t]]
            sup[t] = b.max() if len(b) else np.nan
            res[t] = u.min() if len(u) else np.nan
    return sup, res


def layer(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Per candle and side, which of the 4 checks pass (True/False)."""
    o, h, lo, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    a = atr(df).to_numpy(float)
    sup, res = levels(df)
    prev_sup, prev_res = np.r_[np.nan, sup[:-1]], np.r_[np.nan, res[:-1]]
    vsma = df["volume"].rolling(20, min_periods=20).mean().shift(1).to_numpy(float)
    m = mfi(df, 14)
    low5, high5 = m.rolling(5, min_periods=1).min().to_numpy(float), m.rolling(5, min_periods=1).max().to_numpy(float)
    m = m.to_numpy(float)
    rng = np.where(h - lo > 0, h - lo, np.nan)
    pos = (c - lo) / rng          # 0 bottom .. 1 top
    body = np.abs(c - o) / rng
    with np.errstate(invalid="ignore"):
        big = v >= 1.5 * vsma
        long = pd.DataFrame({
            "level": (lo <= sup + 0.5 * a) | (c > prev_res),
            "volume": big & (c > o),
            "mfi": (low5 < 20) & (m > 20) & (m < 50),
            "candle": (pos >= 2 / 3) & (body >= 0.5),
        }, index=df.index)
        short = pd.DataFrame({
            "level": (h >= res - 0.5 * a) | (c < prev_sup),
            "volume": big & (c < o),
            "mfi": (high5 > 80) & (m < 80) & (m > 50),
            "candle": (pos <= 1 / 3) & (body >= 0.5),
        }, index=df.index)
    return {"long": long.fillna(False), "short": short.fillna(False)}


STATS: dict = {}


class Gated:
    """``base`` whose signals only count when they clear the layer (``need`` of 4 checks)."""

    def __init__(self, base, need: int, sid: str):
        self.base, self.need, self.sid = base, need, sid

    def __getattr__(self, name):
        return getattr(self.base, name)

    def generate(self, df, exchange, symbol, timeframe):
        sigs = self.base.generate(df, exchange, symbol, timeframe)
        if not sigs:
            return sigs
        L = layer(df)
        step = TIMEFRAME_MS[timeframe]
        where = {int(t) + step: i for i, t in enumerate(df["ts"].to_numpy(np.int64))}
        st = STATS.setdefault(self.sid, {"signals": 0, "kept": 0, **{k: 0 for k in CHECKS}})
        out = []
        for s in sigs:
            i = where.get(int(s.ts))
            if i is None:
                continue
            row = L["short" if s.side is Side.SELL else "long"].iloc[i]
            st["signals"] += 1
            for k in CHECKS:
                st[k] += bool(row[k])
            if int(row.sum()) >= self.need:
                st["kept"] += 1
                out.append(s)
        return out


def pick(reg: Registry, market: str, use_ml: bool, most: int):
    """The strategies this engine trades now (paper), best last test first."""
    rows = []
    for e in reg.with_status("paper"):
        if not learns_for(e, market):
            continue
        name = (e.name or "") + " " + e.id
        if not use_ml and "ml_" in name:
            continue
        bt = (e.backtest_futures if market == "futures" else e.backtest) or {}
        rows.append(((bt.get("out_of_sample") or {}).get("expectancy_r", -9), e))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [e for _, e in rows[:most]]


def fmt(m: dict) -> str:
    return (f"trades={m['trades']:4d} win={m['win_rate']:.0%} avg={m['expectancy_r']:+.3f}R "
            f"total={m.get('total_r', 0):+.1f}R maxDD={m['max_drawdown_r']:.1f}R")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["spot", "futures"], default="futures")
    ap.add_argument("--need", type=int, default=3)
    ap.add_argument("--max-strategies", type=int, default=40)
    ap.add_argument("--max-pairs", type=int, default=20)
    ap.add_argument("--no-ml", action="store_true")
    ap.add_argument("--registry", default="data/learn/registry.json")
    ap.add_argument("--out", default="data/reports/mfi_gate_test.json")
    args = ap.parse_args()

    s = Settings.from_env()
    cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                         slippage_bps=s.paper_slippage_bps, max_cost_r=s.max_cost_r)
    reg = Registry(args.registry)
    entries = pick(reg, args.market, not args.no_ml, args.max_strategies)
    store, pairs, costs = _engine_market(s, args.market, s.symbols, cfg)
    pairs = list(pairs)[:args.max_pairs]
    print(f"=== {args.market}: {len(entries)} strategies (paper), {len(pairs)} pairs; layer needs "
          f"{args.need} of 4 checks ===", flush=True)
    original = lab.make_strategy
    rows, tot = [], {"off": [], "on": []}
    for e in entries:
        t0 = time.time()
        res = {}
        for mode in ("off", "on"):
            if mode == "on":
                lab.make_strategy = lambda en, sid=e.id: Gated(original(en), args.need, sid)
            try:
                r = lab.evaluate(e, store, EXCHANGES, pairs, costs, move=False, market=args.market)
            finally:
                lab.make_strategy = original
            res[mode] = r["out_of_sample"]
        st = STATS.get(e.id, {"signals": 0, "kept": 0})
        off, on = res["off"], res["on"]
        print(f"\n  {e.id[:60]}  ({time.time() - t0:.0f}s)\n    OFF {fmt(off)}\n    ON  {fmt(on)}"
              + (f"   | layer let through {st['kept']}/{st['signals']} signals" if st["signals"] else ""),
              flush=True)
        rows.append({"id": e.id, "off": off, "on": on, "layer": st})
        for mode in ("off", "on"):
            tot[mode].append(res[mode])
    store.close()

    def pool(ms):
        n = sum(m["trades"] for m in ms)
        tr = sum(m.get("total_r", 0) for m in ms)
        wins = sum(m["win_rate"] * m["trades"] for m in ms)
        return n, (tr / n if n else 0.0), (wins / n if n else 0.0), tr
    n0, e0, w0, t0_ = pool(tot["off"])
    n1, e1, w1, t1_ = pool(tot["on"])
    sig = sum(r["layer"].get("signals", 0) for r in rows)
    kept = sum(r["layer"].get("kept", 0) for r in rows)
    better = sum(1 for r in rows if r["on"]["trades"] and r["on"]["expectancy_r"] > r["off"]["expectancy_r"])
    print("\n=== Summary (out of sample, after fees) ===")
    print(f"  Layer OFF: {n0} trades, win {w0:.0%}, {e0:+.3f}R per trade, total {t0_:+.1f}R")
    print(f"  Layer ON : {n1} trades, win {w1:.0%}, {e1:+.3f}R per trade, total {t1_:+.1f}R")
    if sig:
        print(f"  The layer let through {kept} of {sig} signals ({kept / sig:.0%}).")
        for k in CHECKS:
            got = sum(r["layer"].get(k, 0) for r in rows)
            print(f"    check '{k}' passed on {got / sig:.0%} of signals")
    print(f"  Profit per trade better with the layer on {better} of {len(rows)} strategies.")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"market": args.market, "need": args.need, "pairs": pairs, "strategies": rows,
                   "summary": {"off": [n0, e0, w0, t0_], "on": [n1, e1, w1, t1_], "signals": sig, "kept": kept}},
                  fh, indent=1, default=str)
    print(f"\nsaved to {args.out} (nothing changed; nothing traded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
