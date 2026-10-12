"""Multi-year backtest report: how every strategy did over the last N years, year by year.

Unlike the backtest gate (``lab.evaluate``), which keeps the last 20% of history untouched to
judge a strategy, this report runs each strategy over the whole window to show its track record:
trades, win rate, average result per trade (R), profit factor, worst drawdown, and the same for
each calendar year, pair and timeframe. Costs (fees and slippage) are included.

Results are in R, the amount risked per trade. "At 1% risk" turns R into USDT for a 10,000 USDT
account risking 1% (100 USDT) per trade, without compounding.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from ..backtest import BacktestConfig, metrics, run_backtest
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from .lab import MIN_BARS
from .registry import Entry, make_strategy

YEAR_MS = 365 * 86_400_000
DEFAULT_PATH = "data/reports/backtest.json"
ACCOUNT_USDT = 10_000.0
RISK_PCT = 1.0


def known_entries(registry_entries: dict, builtins: bool = True) -> list[Entry]:
    """Every strategy the agent knows: the registry, the book library and the built-ins."""
    from ..strategies import STRATEGIES
    from .library import library_specs
    from .rules import spec_id

    entries = dict(registry_entries)
    for spec in library_specs()[0]:
        entries.setdefault(spec_id(spec), Entry(id=spec_id(spec), kind="rule", name=spec["name"], spec=spec,
                                                source=f"{spec['source']['book']} {spec['source']['pages']}".strip()))
    if builtins:
        for name in STRATEGIES:
            entries.setdefault(f"builtin-{name}", Entry(id=f"builtin-{name}", kind="builtin", name=name,
                                                       source=STRATEGIES[name].spec.source))
    return list(entries.values())


def backtest_report(entries: list[Entry], store: CandleStore, exchanges: list[str], symbols: list[str],
                    years: int = 5, cfg: BacktestConfig | None = None, now_ms: int | None = None,
                    progress=None) -> dict:
    cfg = cfg or BacktestConfig()
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - years * YEAR_MS
    frames: dict = {}  # (ex, sym, tf) -> DataFrame, loaded once for every strategy
    rows, skipped = [], []
    for n, entry in enumerate(entries, 1):
        try:
            strat = make_strategy(entry)
        except Exception as exc:
            skipped.append({"id": entry.id, "why": f"can't build it ({type(exc).__name__})"})
            continue
        trades, markets = [], []
        for ex in exchanges:
            for sym in symbols:
                for tf in strat.spec.timeframes:
                    key = (ex, sym, tf)
                    if key not in frames:
                        candles = store.load(ex, sym, tf, start=since)
                        frames[key] = candles_to_frame(candles) if len(candles) >= MIN_BARS else None
                    df = frames[key]
                    if df is None:
                        continue
                    t = run_backtest(strat, df, ex, sym, tf, cfg)
                    trades += t
                    markets.append({"exchange": ex, "symbol": sym, "timeframe": tf, "bars": len(df),
                                    "from": int(df["ts"].iloc[0]), "to": int(df["ts"].iloc[-1]),
                                    "trades": len(t), "total_r": round(sum(x.r for x in t), 2)})
        if progress:
            progress(n, len(entries), entry.id, len(trades))
        if not markets:
            skipped.append({"id": entry.id, "why": "no price history for its timeframes "
                                                   + ", ".join(strat.spec.timeframes)})
            continue
        rows.append(_summary(entry, trades, markets, now_ms))
    rows.sort(key=lambda r: (r["all"]["total_r"], r["all"]["expectancy_r"]), reverse=True)
    covered = [m for r in rows for m in r["markets"]]
    return {
        "generated": now_ms, "years": years, "since": since,
        "data_from": min((m["from"] for m in covered), default=None),
        "data_to": max((m["to"] for m in covered), default=None),
        "exchanges": exchanges, "symbols": symbols,
        "costs": {"taker_fee": cfg.taker_fee, "maker_fee": cfg.maker_fee, "slippage_bps": cfg.slippage_bps},
        "account_usdt": ACCOUNT_USDT, "risk_pct": RISK_PCT,
        "strategies": rows, "skipped": skipped,
        "totals": _totals(rows),
    }


def _summary(entry: Entry, trades: list, markets: list[dict], now_ms: int) -> dict:
    by_year, by_symbol = defaultdict(list), defaultdict(list)
    for t in trades:
        by_year[datetime.fromtimestamp(t.exit_ts / 1000, timezone.utc).year].append(t)
        by_symbol[t.symbol].append(t)
    last_year = [t for t in trades if t.exit_ts >= now_ms - YEAR_MS]
    m = _m(trades)
    years = {str(y): _m(ts) for y, ts in sorted(by_year.items())}
    good_years = sum(1 for v in years.values() if v["total_r"] > 0)
    return {
        "id": entry.id, "name": entry.name, "source": entry.source, "status": entry.status,
        "timeframes": sorted({mk["timeframe"] for mk in markets}),
        "all": m, "last_12_months": _m(last_year), "by_year": years,
        "by_symbol": {s: _m(ts) for s, ts in sorted(by_symbol.items())},
        "profitable_years": good_years, "years_traded": len(years),
        "verdict": _verdict(m, good_years, len(years)),
        "usdt_at_1pct": round(m["total_r"] * ACCOUNT_USDT * RISK_PCT / 100, 2),
        "markets": markets,
    }


def _m(trades: list) -> dict:
    out = metrics(trades)
    pf = out["profit_factor"]
    return {"trades": out["trades"], "wins": int(round(out["win_rate"] * out["trades"])),
            "win_rate": round(out["win_rate"] * 100, 1), "expectancy_r": round(out["expectancy_r"], 3),
            "profit_factor": None if pf == float("inf") else round(pf, 2),
            "max_drawdown_r": round(out["max_drawdown_r"], 1), "total_r": round(out["total_r"], 1)}


def _verdict(m: dict, good_years: int, years: int) -> str:
    if m["trades"] < 30:
        return "too few trades to judge"
    pf = m["profit_factor"] or 99
    if m["expectancy_r"] > 0.05 and pf >= 1.2 and good_years >= max(1, years - 1):
        return "strong: profitable almost every year"
    if m["expectancy_r"] > 0 and pf > 1.0:
        return "profitable overall, uneven by year" if good_years < years - 1 else "slightly profitable"
    return "lost money after costs"


def _totals(rows: list[dict]) -> dict:
    tested = len(rows)
    profitable = sum(1 for r in rows if r["all"]["total_r"] > 0)
    strong = sum(1 for r in rows if r["verdict"].startswith("strong"))
    trades = sum(r["all"]["trades"] for r in rows)
    wins = sum(r["all"]["wins"] for r in rows)
    return {"tested": tested, "profitable": profitable, "strong": strong, "trades": trades,
            "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
            "best": rows[0]["id"] if rows else None}


def save_report(report: dict, path: str = DEFAULT_PATH) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    os.replace(tmp, path)
    return path


def report_text(report: dict, top: int = 60) -> str:
    """Plain-text table for the terminal."""
    lines = [f"{report['years']}-year backtest, {report['totals']['tested']} strategies, "
             f"{report['totals']['trades']} trades, overall win rate {report['totals']['win_rate']}%",
             f"{'strategy':44} {'trades':>6} {'win%':>6} {'R/trade':>8} {'PF':>5} {'total R':>8} "
             f"{'maxDD':>6} {'good yrs':>8}  verdict"]
    for r in report["strategies"][:top]:
        a = r["all"]
        lines.append(f"{r['id'][:44]:44} {a['trades']:>6} {a['win_rate']:>6.1f} {a['expectancy_r']:>+8.3f} "
                     f"{(a['profit_factor'] or 0):>5.2f} {a['total_r']:>+8.1f} {a['max_drawdown_r']:>6.1f} "
                     f"{r['profitable_years']:>3}/{r['years_traded']:<4}  {r['verdict']}")
    for s in report["skipped"]:
        lines.append(f"  skipped {s['id']}: {s['why']}")
    return "\n".join(lines)
