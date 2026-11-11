"""Test-only: support/resistance + volume + demand strategies, run through the platform's own
backtest gate on this server's stored Bybit candles. Nothing is added to the registry and nothing
trades: it only prints (and saves) the results next to the best strategies you already have.

Run on the server (inside the platform's container, so it reads the same data):

    sudo docker compose run --rm -v ~/try_sr_volume.py:/app/try_sr_volume.py \
        --entrypoint python collector /app/try_sr_volume.py

Options: --market spot|futures|both (default both), --tf 1h,4h (default), --max-pairs N (default 20),
--no-ml (skip the slower ML version).

The strategies (thresholds chosen before any test, not tuned):

* sr_bounce: price dips into a support zone (swing lows/highs touched at least twice, or the
  busiest price of the last 200 bars) and closes back up in the top of its range on at least 1.5x
  normal volume with money flowing in (CMF > 0). Stop below the zone; target the next resistance
  (at least 1.5R away, else no trade); time stop 30 bars.
* sr_breakout_retest: a close through resistance on at least 2x normal volume, then within 6 bars
  price comes back to the broken level and closes above it again. Stop 1 ATR under the level, 2R.
* ml_sr_volume: gradient-boosted trees (walk-forward, like ml_boosted) that learn from the zone
  and demand numbers (distance to support/resistance in ATR, zone strength, volume, close
  location, CMF, OBV slope, distance to the busiest price, open-interest change on futures);
  trades near a support zone when the chance of +3 ATR before -1.5 ATR is 70% or more.

Each also has its short twin (the same rules on the upside-down chart: bounce at resistance,
breakdown and retest), tested on Futures only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from aitrader.backtest import BacktestConfig
from aitrader.cli import EXCHANGES, _engine_market
from aitrader.config import Settings
from aitrader.indicators import atr, cmf, ema, obv
from aitrader.learn import Entry, Registry, evaluate
from aitrader.strategies import STRATEGIES
from aitrader.strategies.base import StrategySpec
from aitrader.strategies.families import BASE, NO_TRADE, _strong_downtrend
from aitrader.strategies.master import TREND_RANGE, _df_key, _Master
from aitrader.strategies.ml import MlBoosted, feature_frame

SOURCE = "Shawn's ask 2026-11-11: volume, demand, support and resistance (test only)"
PIVOT = 5          # a swing high/low: the highest/lowest bar of 5 bars each side (known 5 bars later)
LEVELS = 40        # the most recent swing points kept as levels
POC_BARS = 200     # busiest price of the last 200 bars
POC_EVERY = 12     # recomputed every 12 bars


# ---- the numbers: zones and demand, using only bars already closed ------------------------------------


def zones(df: pd.DataFrame) -> pd.DataFrame:
    """Per bar: nearest support below the close and resistance above it, how many swing points sit
    in each zone (half an ATR wide), and the distance to the busiest price of the last 200 bars."""
    h, lo, c, v = (df[k].to_numpy(float) for k in ("high", "low", "close", "volume"))
    a = atr(df).to_numpy(float)
    n = len(df)
    sup, res, s_str, r_str, poc = (np.full(n, np.nan) for _ in range(5))
    levels: list[float] = []
    poc_now = np.nan
    for t in range(n):
        p = t - PIVOT  # this swing point is confirmed now
        if p >= PIVOT:
            win = slice(p - PIVOT, p + PIVOT + 1)
            if h[p] >= h[win].max():
                levels.append(h[p])
            if lo[p] <= lo[win].min():
                levels.append(lo[p])
            if len(levels) > LEVELS:
                levels = levels[-LEVELS:]
        if t >= POC_BARS and t % POC_EVERY == 0:
            w = slice(t - POC_BARS + 1, t + 1)
            typ = (h[w] + lo[w] + c[w]) / 3
            edges = np.linspace(lo[w].min(), h[w].max(), 25)
            idx = np.clip(np.digitize(typ, edges) - 1, 0, 23)
            vol = np.bincount(idx, weights=v[w], minlength=24)
            b = int(np.argmax(vol))
            poc_now = (edges[b] + edges[b + 1]) / 2
        poc[t] = poc_now
        if not levels or not np.isfinite(a[t]) or a[t] <= 0:
            continue
        lv = np.array(levels + ([poc_now] if np.isfinite(poc_now) else []))
        tol = 0.5 * a[t]
        below, above = lv[lv < c[t]], lv[lv > c[t]]
        if len(below):
            s = below.max()
            sup[t] = s
            s_str[t] = np.sum(np.abs(lv - s) <= tol) + (1 if np.isfinite(poc_now) and abs(poc_now - s) <= tol else 0)
        if len(above):
            r = above.min()
            res[t] = r
            r_str[t] = np.sum(np.abs(lv - r) <= tol) + (1 if np.isfinite(poc_now) and abs(poc_now - r) <= tol else 0)
    return pd.DataFrame({"support": sup, "resistance": res, "s_strength": s_str, "r_strength": r_str,
                         "poc": poc}, index=df.index)


def demand(df: pd.DataFrame) -> pd.DataFrame:
    """Volume against normal, where the bar closed in its range (+1 top, -1 bottom), money flow
    (CMF 20) and the OBV slope over 10 bars scaled by normal volume."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    vol_avg = df["volume"].rolling(20, min_periods=20).mean().shift(1)
    out = pd.DataFrame(index=df.index)
    out["rvol"] = df["volume"] / vol_avg
    out["clv"] = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    out["cmf"] = cmf(df, 20)
    out["obv_slope"] = (obv(df) - obv(df).shift(10)) / (vol_avg * 10)
    return out


def _cached(self, df):
    key = _df_key(df)
    if getattr(self, "_zkey", None) != key:
        self._zkey, self._z, self._d = key, zones(df), demand(df)
    return self._z, self._d


def _spec(**kw) -> StrategySpec:
    return StrategySpec(source=SOURCE, size="0.5% of equity at risk.", filter=NO_TRADE, **kw)


# ---- the rule strategies ---------------------------------------------------------------------------


class SrBounce(_Master):
    spec = _spec(
        name="sr_bounce", timeframes=("1h", "4h"),
        regime="Range or uptrend; never in a strong downtrend (ADX > 30 with sellers on top).",
        setup="The bar's low reaches a support zone (within 0.25 ATR; 2+ swing points or the busiest "
        "price) and it closes above the zone, in the top 35% of its range, up on the bar, on >= 1.5x "
        "the 20-bar volume, with CMF(20) > 0.",
        entry="Market order at the next bar's open.",
        stop="Support minus 0.5 x ATR(14) (or the bar's low minus 0.25 ATR if lower).",
        target="The next resistance zone, only if it is at least 1.5R away; time stop 30 bars.")
    eligible_regimes = ("trend-up", "range", "uncertain")
    reason = "bounced off support on high volume with buyers in control"

    @classmethod
    def default_params(cls):
        return {**BASE, "touch_atr": 0.25, "min_strength": 2, "rvol": 1.5, "clv": 0.3, "stop_atr": 0.5,
                "min_rr": 1.5, "time_stop": 30}

    def setup(self, df, timeframe):
        z, d = _cached(self, df)
        p, a = self.params, atr(df)
        sup = z["support"]
        return ((df["low"] <= sup + p["touch_atr"] * a) & (df["close"] > sup) & (df["close"] > df["open"])
                & (z["s_strength"] >= p["min_strength"]) & (d["clv"] >= p["clv"]) & (d["rvol"] >= p["rvol"])
                & (d["cmf"] > 0) & ~_strong_downtrend(df, 30)).fillna(False)

    def stop_series(self, df):
        z, _ = _cached(self, df)
        a = atr(df)
        return np.minimum(z["support"] - self.params["stop_atr"] * a, df["low"] - 0.25 * a)

    def targets(self, df):
        z, _ = _cached(self, df)
        res = z["resistance"].to_numpy(float)
        rr = self.params["min_rr"]

        def target(i, entry, stop):
            t = res[i]
            if not np.isfinite(t) or t - entry < rr * (entry - stop):
                return None
            return t
        return target

    def exit_key(self):
        return f"time_stop={self.params['time_stop']}"

    def features(self, df):
        z, d = _cached(self, df)
        return {"rvol": d["rvol"], "cmf": d["cmf"], "zone_strength": z["s_strength"]}


class SrBreakoutRetest(_Master):
    spec = _spec(
        name="sr_breakout_retest", timeframes=("1h", "4h"),
        regime="Uptrend or forming trend: close above EMA(200).",
        setup="Breakout: a close more than 0.1 ATR above a resistance zone (2+ swing points or the "
        "busiest price) after closing under it, on >= 2x the 20-bar volume, top half of its range, "
        "CMF(20) > 0. Retest: within the next 6 bars the low comes back within 0.3 ATR of the broken "
        "level and the bar closes above it, up on the bar.",
        entry="Market order at the next bar's open.",
        stop="The broken level minus 1 x ATR(14).",
        target="2R fixed; time stop 30 bars.")
    eligible_regimes = TREND_RANGE
    reason = "broke resistance on high volume and held it on the retest"

    @classmethod
    def default_params(cls):
        return {**BASE, "min_strength": 2, "rvol": 2.0, "clv": 0.0, "window": 6, "retest_atr": 0.3,
                "stop_atr": 1.0, "target_r": 2.0, "time_stop": 30}

    def _levels(self, df):
        key = _df_key(df)
        if getattr(self, "_lkey", None) == key:
            return self._lv
        z, d = _cached(self, df)
        p = self.params
        c, o, lo = (df[k].to_numpy(float) for k in ("close", "open", "low"))
        a = atr(df).to_numpy(float)
        res, strength = z["resistance"].to_numpy(float), z["r_strength"].to_numpy(float)
        rvol, clv, flow = (d[k].to_numpy(float) for k in ("rvol", "clv", "cmf"))
        e200 = ema(df["close"], 200).to_numpy(float)
        n = len(df)
        setup, level = np.zeros(n, bool), np.full(n, np.nan)
        broken, until = np.nan, -1
        for t in range(1, n):
            lvl = res[t - 1]  # the resistance the previous close sat under
            if (np.isfinite(lvl) and np.isfinite(a[t]) and c[t - 1] <= lvl and c[t] > lvl + 0.1 * a[t]
                    and strength[t - 1] >= p["min_strength"] and rvol[t] >= p["rvol"]
                    and clv[t] >= p["clv"] and flow[t] > 0 and c[t] > e200[t]):
                broken, until = lvl, t + int(p["window"])
                continue
            if t <= until and np.isfinite(broken):
                if c[t] < broken - 0.5 * a[t]:  # fell back through: the breakout failed
                    broken, until = np.nan, -1
                elif lo[t] <= broken + p["retest_atr"] * a[t] and c[t] > broken and c[t] > o[t]:
                    setup[t], level[t] = True, broken
                    broken, until = np.nan, -1
        self._lkey, self._lv = key, (pd.Series(setup, index=df.index), pd.Series(level, index=df.index))
        return self._lv

    def setup(self, df, timeframe):
        return self._levels(df)[0]

    def stop_series(self, df):
        return self._levels(df)[1] - self.params["stop_atr"] * atr(df)

    def exit_key(self):
        return f"time_stop={self.params['time_stop']}"


# ---- the ML version: learns which mix of those numbers paid -----------------------------------------


def sr_features(df: pd.DataFrame, z: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    a = atr(df).replace(0, np.nan)
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    out["dist_support_atr"] = (c - z["support"]) / a
    out["dist_resist_atr"] = (z["resistance"] - c) / a
    out["support_strength"] = z["s_strength"]
    out["resist_strength"] = z["r_strength"]
    out["dist_poc_atr"] = (c - z["poc"]) / a
    for k in ("rvol", "clv", "cmf", "obv_slope"):
        out[k] = d[k]
    out["rvol_3"] = d["rvol"].rolling(3).mean()
    if "open_interest" in df.columns:  # futures: new money coming in or leaving
        oi = df["open_interest"].astype(float)
        out["oi_chg_6"] = oi.pct_change(6, fill_method=None)
        out["oi_chg_24"] = oi.pct_change(24, fill_method=None)
    if "funding_rate" in df.columns:
        out["funding"] = df["funding_rate"].astype(float)
    return out


class MlSrVolume(MlBoosted):
    spec = _spec(
        name="ml_sr_volume", timeframes=("1h", "4h"),
        regime="Any orderly market; the model learns which conditions pay.",
        setup="Gradient-boosted trees (walk-forward, retrained every 250 bars on up to 1,500) read the "
        "standard 25 inputs plus the zone and demand numbers (distance to support/resistance in ATR, "
        "zone strength, distance to the busiest price, relative volume, close location, CMF, OBV slope, "
        "open-interest change and funding on futures) and estimate the chance of +3 ATR before -1.5 "
        "ATR within 24 bars. Trades when that chance crosses 70% while price is within 1 ATR of support.",
        entry="Market order at the next bar's open.",
        stop="Entry - 1.5 x ATR(14).",
        target="2R (+3 x ATR); time stop after 24 bars.")
    reason = "ML: zone and demand numbers give >= 70% chance of +2R"
    needs_context = True  # open interest and funding on the perpetuals, when stored

    def _features(self, df):
        z, d = _cached(self, df)
        X = pd.concat([feature_frame(df), sr_features(df, z, d)], axis=1)
        # inputs that only exist on futures may be missing on a chart: never let them block a row
        must = X.drop(columns=[c for c in ("oi_chg_6", "oi_chg_24", "funding") if c in X.columns])
        return X.to_numpy(float), must.notna().all(axis=1).to_numpy()

    def _model(self, X, y, key):
        X = np.where(np.isfinite(X), X, np.nan)  # HistGradientBoosting handles NaN itself
        return super()._model(X, y, key)

    def setup(self, df, timeframe):
        near = ((df["close"] - _cached(self, df)[0]["support"]) <= atr(df)).fillna(False)
        return super().setup(df, timeframe) & near


# ---- run -------------------------------------------------------------------------------------------


NEW = {"sr_bounce": SrBounce, "sr_breakout_retest": SrBreakoutRetest, "ml_sr_volume": MlSrVolume}


def entries(use_ml: bool, market: str) -> list[Entry]:
    out = []
    for name in NEW:
        if name == "ml_sr_volume" and not use_ml:
            continue
        long = Entry(id=f"try-{name}", kind="builtin", name=name, spec={}, source=SOURCE)
        if market == "spot":
            out.append(long)
        else:
            out += [long, Entry(id=f"try-short-{name}", kind="short", name=f"short: {name}", source=SOURCE,
                                spec={"base_id": long.id, "base_kind": "builtin", "base_name": name, "base_spec": {}})]
    return out


def best_existing(reg: Registry, market: str, n: int = 10) -> list[tuple]:
    rows = []
    for e in reg.entries.values():
        bt = (e.backtest_futures if market == "futures" else e.backtest) or {}
        if market == "futures" and not bt:
            continue
        if market == "spot" and e.kind == "short":
            continue
        m = bt.get("out_of_sample") or {}
        if (m.get("trades") or 0) < 30:
            continue
        rows.append((m.get("expectancy_r", 0.0), m.get("trades", 0), m.get("win_rate", 0.0),
                     m.get("profit_factor", 0.0), e.id, e.status, not bt.get("failed")))
    return sorted(rows, reverse=True)[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["spot", "futures", "both"], default="both")
    ap.add_argument("--tf", default="1h,4h")
    ap.add_argument("--max-pairs", type=int, default=20)
    ap.add_argument("--no-ml", action="store_true")
    ap.add_argument("--registry", default="data/learn/registry.json")
    ap.add_argument("--out", default="data/reports/sr_volume_test.json")
    args = ap.parse_args()
    tfs = tuple(t.strip() for t in args.tf.split(",") if t.strip())
    for cls in NEW.values():
        cls.spec = replace(cls.spec, timeframes=tfs)
    STRATEGIES.update(NEW)  # in this process only: the registry file is never written

    s = Settings.from_env()
    cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                         slippage_bps=s.paper_slippage_bps, max_cost_r=s.max_cost_r)
    reg = Registry(args.registry) if os.path.exists(args.registry) else None
    report = {"made": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "timeframes": tfs, "markets": {}}
    for market in (("spot", "futures") if args.market == "both" else (args.market,)):
        store, pairs, costs = _engine_market(s, market, s.symbols, cfg)
        pairs = list(pairs)[:args.max_pairs]
        print(f"\n=== {'Spot AI (spot candles)' if market == 'spot' else 'Futures AI (perpetual candles)'}: "
              f"{len(pairs)} pairs, {', '.join(tfs)} ===", flush=True)
        results = []
        for e in entries(not args.no_ml, market):
            t0 = time.time()
            print(f"  testing {e.id} ...", flush=True)
            res = evaluate(e, store, EXCHANGES, pairs, costs, move=False, market=market)
            m, wf, h = res["out_of_sample"], res["walk_forward"], res["holdout"]
            verdict = "no data" if not res["markets"] else "FAILED" if res["failed"] else "PASSED"
            print(f"  {e.id:32} {verdict:8} oos trades={m['trades']:4d} win={m['win_rate']:.0%} "
                  f"exp={m['expectancy_r']:+.3f}R pf={m['profit_factor']:.2f} dd={m['max_drawdown_r']:.1f}R "
                  f"walk-forward {wf['positive']}/{wf['counted']}; holdout "
                  + (f"{h['trades']} trades {h['expectancy_r']:+.3f}R" if h["evaluated"] else "not looked at")
                  + f"  ({time.time() - t0:.0f}s)"
                  + (f"\n      why: {'; '.join(res['failed'])}" if res["failed"] else ""), flush=True)
            results.append({"id": e.id, "verdict": verdict, "out_of_sample": m, "walk_forward": wf,
                            "holdout": h, "failed": res["failed"],
                            "by_pair": [{k: mk[k] for k in ("symbol", "timeframe", "trades_oos", "exp_oos")}
                                        for mk in res["markets"]]})
        store.close()
        best = best_existing(reg, market) if reg else []
        if best:
            print(f"\n  Your best existing {market} strategies (their last test, out of sample, 30+ trades):")
            for exp, n, wr, pf, sid, status, passed in best:
                print(f"  {sid[:40]:40} {status:13} trades={n:4d} win={wr:.0%} exp={exp:+.3f}R pf={pf:.2f}"
                      f"{'' if passed else '  (failed the gate)'}")
        report["markets"][market] = {"pairs": pairs, "new": results,
                                     "best_existing": [dict(zip(("expectancy_r", "trades", "win_rate", "profit_factor",
                                                                 "id", "status", "passed"), b)) for b in best]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"\nsaved to {args.out} (nothing was added to your strategies; nothing traded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
