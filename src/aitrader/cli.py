"""Command line entry point: ``aitrader <command>``. Run ``aitrader --help`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from .config import Settings
from .data.downloader import download
from .data.feeds import LiveFeed
from .data.quality import check_candles
from .data.store import CandleStore
from .exchanges import PaperExchange, make_adapter
from .models import OrderType, Side
from .safety import live_gate_problems

EXCHANGES = ["binance", "htx"]


def _ms(date: str) -> int:
    dt = datetime.fromisoformat(date)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _fmt(ts: int | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def cmd_status(s: Settings, _args) -> int:
    print(f"mode: {s.mode.value}")
    problems = live_gate_problems(s)
    print("live trading: " + ("UNLOCKED" if not problems else "locked"))
    for p in problems:
        print(f"  - {p}")
    for name, k in s.keys.items():
        print(f"keys[{name}]: {'set' if k.present else 'not set'}")
    print(f"db: {s.db_path}")
    print(f"symbols: {', '.join(s.symbols)}")
    print(f"timeframes: {', '.join(s.timeframes)}")
    return 0


async def cmd_download(s: Settings, args) -> int:
    store = CandleStore(s.db_path)
    since = _ms(args.since)
    until = _ms(args.until) if args.until else None
    exchanges = args.exchange or EXCHANGES
    for ex in exchanges:
        adapter = make_adapter(ex, s)
        try:
            for sym in args.symbol or s.symbols:
                for tf in args.timeframe or s.timeframes:
                    try:
                        n = await download(adapter, store, sym, tf, since, until)
                        print(f"{ex} {sym} {tf}: +{n} candles")
                    except Exception as exc:
                        print(f"{ex} {sym} {tf}: FAILED {exc}", file=sys.stderr)
        finally:
            await adapter.close()
    store.close()
    return 0


def cmd_check_data(s: Settings, args) -> int:
    store = CandleStore(s.db_path)
    bad = 0
    for ex, sym, tf, n, lo, hi in store.summary():
        rep = check_candles(store.load(ex, sym, tf), tf)
        flag = "OK " if rep.ok else "BAD"
        bad += not rep.ok
        print(f"[{flag}] {ex:8} {sym:10} {tf:4} {_fmt(lo)} -> {_fmt(hi)}  {rep.as_text()}")
    store.close()
    return 1 if bad and args.strict else 0


async def cmd_collect(s: Settings, args) -> int:
    """Long-running service: keep the candle database topped up from both exchanges."""
    import time

    log = logging.getLogger("aitrader.collect")
    store = CandleStore(s.db_path)
    exchanges = args.exchange or EXCHANGES
    adapters = {ex: make_adapter(ex, s) for ex in exchanges}
    try:
        while True:
            since = int(time.time() * 1000) - args.backfill_days * 86_400_000
            for ex, adapter in adapters.items():
                for sym in s.symbols:
                    for tf in s.timeframes:
                        try:
                            n = await download(adapter, store, sym, tf, since)
                            if n:
                                log.info("%s %s %s +%d", ex, sym, tf, n)
                        except Exception as exc:
                            log.warning("%s %s %s failed: %s", ex, sym, tf, exc)
            await asyncio.sleep(args.interval)
    finally:
        for a in adapters.values():
            await a.close()
        store.close()


async def cmd_stream(s: Settings, args) -> int:
    def on_tick(ex, sym, px, ts):
        print(f"{_fmt(ts)} {ex} {sym} {px}")

    feed = LiveFeed(args.exchange, args.symbol or s.symbols, on_tick)
    task = asyncio.create_task(feed.run())
    try:
        await asyncio.wait_for(task, timeout=args.seconds) if args.seconds else await task
    except asyncio.TimeoutError:
        pass
    return 0


async def cmd_paper_smoke(s: Settings, args) -> int:
    """Phase 1 exit check: open and close a small position on the paper exchange with live prices."""
    market = make_adapter(args.exchange, s)
    paper = PaperExchange(market, s)
    try:
        ob = await paper.get_orderbook(args.symbol)
        amount = round(args.usdt / ob.best_ask, 6)
        buy = await paper.place_order(args.symbol, Side.BUY, amount, OrderType.MARKET, reason="smoke test")
        print(f"BUY  {buy.status.value} {buy.filled} @ {buy.average:.2f} fee={buy.fee:.4f}")
        print(f"positions: {await paper.positions()}")
        sell = await paper.place_order(args.symbol, Side.SELL, amount, OrderType.MARKET, reason="smoke test")
        print(f"SELL {sell.status.value} {sell.filled} @ {sell.average:.2f} fee={sell.fee:.4f}")
        print(f"equity: {paper.equity():.2f} USDT (started {s.paper_start_usdt:.2f})")
        return 0 if not await paper.positions() else 1
    finally:
        await market.close()


def cmd_signals(s: Settings, args) -> int:
    """Run a strategy over stored candles and write its signal log (Phase 2 exit check)."""
    import csv

    from .indicators import candles_to_frame
    from .strategies import STRATEGIES

    store = CandleStore(s.db_path)
    candles = store.load(args.exchange, args.symbol, args.timeframe,
                         _ms(args.since) if args.since else None)
    store.close()
    if not candles:
        print("no candles stored for that series; run `aitrader download` first", file=sys.stderr)
        return 1
    strat = STRATEGIES[args.strategy]()
    sigs = strat.generate(candles_to_frame(candles), args.exchange, args.symbol, args.timeframe)
    rows = [sig.as_row() for sig in sigs]
    out = args.out or f"data/signals_{args.strategy}_{args.exchange}_{args.symbol.replace('/', '')}_{args.timeframe}.csv"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["ts"])
        w.writeheader()
        w.writerows(rows)
    for sig in sigs[-args.show:]:
        print(f"{_fmt(sig.ts)} {sig.side.value} {sig.symbol} entry={sig.entry:.6g} "
              f"stop={sig.stop:.6g} target={sig.target:.6g}  {sig.reason}")
    print(f"{len(sigs)} signals over {len(candles)} bars -> {out}")
    return 0


def cmd_backtest(s: Settings, args) -> int:
    """Backtest one strategy on stored candles: in-sample / out-of-sample / final-test split,
    monthly results calendar and a trades CSV (Phase 3)."""
    import pandas as pd

    from .backtest import BacktestConfig, metrics, results_calendar, run_backtest, split_periods
    from .backtest.engine import trades_frame
    from .indicators import candles_to_frame
    from .strategies import STRATEGIES

    cfg = BacktestConfig(taker_fee=args.taker_fee, maker_fee=args.maker_fee,
                         slippage_bps=args.slippage_bps, funding_rate_8h=args.funding_8h)
    strat_cls = STRATEGIES[args.strategy]
    store = CandleStore(s.db_path)
    status = 0
    for sym in args.symbol or s.symbols:
        candles = store.load(args.exchange, sym, args.timeframe)
        if len(candles) < 300:
            print(f"{sym} {args.timeframe}: only {len(candles)} candles stored, skipping")
            continue
        df = candles_to_frame(candles)
        print(f"\n== {args.strategy} {args.exchange} {sym} {args.timeframe}: "
              f"{_fmt(int(df.ts.iloc[0]))} -> {_fmt(int(df.ts.iloc[-1]))}, {len(df)} bars")
        ins, oos, final = split_periods(df)
        parts = [("in-sample", ins), ("out-of-sample", oos)]
        if args.final:
            parts.append(("FINAL TEST", final))
        for label, part in parts:
            m = metrics(run_backtest(strat_cls(), part, args.exchange, sym, args.timeframe, cfg))
            print(f"  {label:14} trades={m['trades']:4d} exp={m['expectancy_r']:+.3f}R "
                  f"win={m['win_rate']:.0%} PF={m['profit_factor']:.2f} "
                  f"maxDD={m['max_drawdown_r']:.1f}R costs={m['cost_r_per_trade']:.3f}R/trade"
                  + ("" if m["reliable"] else "  (under 100 trades: not reliable)"))
        trades = run_backtest(strat_cls(), pd.concat([ins, oos]), args.exchange, sym, args.timeframe, cfg)
        cal = results_calendar(trades, "M")
        if not cal.empty:
            print("  month    trades  total_R  win")
            for row in cal.tail(args.months).itertuples():
                print(f"  {row.period}  {row.trades:6d}  {row.total_r:+7.2f}  {row.win_rate:.0%}")
        out = f"data/trades_{args.strategy}_{args.exchange}_{sym.replace('/', '')}_{args.timeframe}.csv"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        trades_frame(trades).to_csv(out, index=False)
        print(f"  trades -> {out}")
    store.close()
    if not args.final:
        print("\nThe final test period was left untouched. Run with --final only once you have "
              "stopped tuning.")
    return status


def cmd_strategies(_s: Settings, _args) -> int:
    from .strategies import STRATEGIES

    for cls in STRATEGIES.values():
        sp = cls.spec
        print(f"== {sp.name} ({sp.source}); timeframes {', '.join(sp.timeframes)}")
        for label in ("setup", "entry", "stop", "target", "filter", "size"):
            print(f"  {label:7} {getattr(sp, label)}")
        print()
    return 0


def cmd_calendar(_s: Settings, args) -> int:
    from .events import CalendarFilter

    cal = CalendarFilter()
    ok = cal.refresh(force=True)
    now = datetime.now(timezone.utc)
    if not ok:
        print("calendar feed unavailable")
    for ev in cal.upcoming(now, args.hours):
        print(f"{ev.time:%a %Y-%m-%d %H:%M} UTC  {ev.country:4} {ev.impact:6} {ev.title}"
              f"  (forecast {ev.forecast or '-'}, previous {ev.previous or '-'})")
    d = cal.decide(now)
    print(f"now: entries {'allowed' if d.allow_entries else 'BLOCKED'}, size x{d.size_multiplier:g}: {d.reason}")
    return 0


def cmd_kb(_s: Settings, args) -> int:
    from .knowledge import DEFAULT_INDEX, KnowledgeBase, build_index

    if args.kb_cmd == "build":
        res = build_index(args.paths or None, out=args.index, include_notes=not args.no_notes)
        for d in res["documents"]:
            print(f"indexed  {d['title'][:60]:60} {d['pages']:4} parts {d['chunks']:5} passages  ({d['path']})")
        for dup, orig in res["duplicates"]:
            print(f"skipped  {dup}: identical to {orig}")
        for path, why in res["skipped"]:
            print(f"skipped  {path}: {why}")
        print(f"{res['chunks']} passages from {len(res['documents'])} documents -> {res['index']}")
        return 0 if res["documents"] else 1
    if not os.path.exists(args.index):
        print(f"no index at {args.index}; run `aitrader kb build` first (default {DEFAULT_INDEX})")
        return 1
    kb = KnowledgeBase.load(args.index)
    hits = kb.search(" ".join(args.query), k=args.k, source=args.source)
    if not hits:
        print("no matches")
        return 1
    for n, h in enumerate(hits, 1):
        text = h.chunk.text if args.full else h.chunk.text[:400] + ("..." if len(h.chunk.text) > 400 else "")
        print(f"[{n}] {h.cite()}  (score {h.score:.1f})\n    {text}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aitrader", description=__doc__)
    p.add_argument("--env-file", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show mode, live-gate state and which keys are set")

    d = sub.add_parser("download", help="download historical candles into the database")
    d.add_argument("--since", required=True, help="UTC date, e.g. 2024-01-01")
    d.add_argument("--until", help="UTC date (default: now)")
    d.add_argument("--exchange", action="append", choices=EXCHANGES)
    d.add_argument("--symbol", action="append")
    d.add_argument("--timeframe", action="append")

    c = sub.add_parser("check-data", help="run data-quality checks on stored candles")
    c.add_argument("--strict", action="store_true", help="exit 1 if any series has problems")

    co = sub.add_parser("collect", help="service: keep candles up to date in the database")
    co.add_argument("--exchange", action="append", choices=EXCHANGES)
    co.add_argument("--interval", type=float, default=60, help="seconds between top-ups")
    co.add_argument("--backfill-days", type=int, default=30, help="history to fetch on first run")

    st = sub.add_parser("stream", help="print live prices from the websocket feed")
    st.add_argument("--exchange", default="binance", choices=EXCHANGES)
    st.add_argument("--symbol", action="append")
    st.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")

    ps = sub.add_parser("paper-smoke", help="open and close a test position on the paper exchange")
    ps.add_argument("--exchange", default="binance", choices=EXCHANGES)
    ps.add_argument("--symbol", default="BTC/USDT")
    ps.add_argument("--usdt", type=float, default=100.0)

    sg = sub.add_parser("signals", help="run a strategy over stored candles and save its signal log")
    sg.add_argument("--strategy", required=True)
    sg.add_argument("--exchange", default="binance", choices=EXCHANGES)
    sg.add_argument("--symbol", default="BTC/USDT")
    sg.add_argument("--timeframe", default="4h")
    sg.add_argument("--since", help="UTC date")
    sg.add_argument("--out", help="CSV path")
    sg.add_argument("--show", type=int, default=10, help="print the last N signals")

    sub.add_parser("strategies", help="print every strategy's rule card")

    bt = sub.add_parser("backtest", help="backtest a strategy on stored candles (fees + slippage)")
    bt.add_argument("--strategy", required=True)
    bt.add_argument("--exchange", default="binance", choices=EXCHANGES)
    bt.add_argument("--symbol", action="append")
    bt.add_argument("--timeframe", default="4h")
    bt.add_argument("--taker-fee", type=float, default=0.001)
    bt.add_argument("--maker-fee", type=float, default=0.001)
    bt.add_argument("--slippage-bps", type=float, default=5.0)
    bt.add_argument("--funding-8h", type=float, default=0.0, help="perps funding per 8h (0 for spot)")
    bt.add_argument("--months", type=int, default=12, help="months of the results calendar to print")
    bt.add_argument("--final", action="store_true", help="also run the untouched final test period")

    ca = sub.add_parser("calendar", help="show upcoming high-impact events and the current decision")
    ca.add_argument("--hours", type=float, default=72)

    kb = sub.add_parser("kb", help="knowledge base: index the books and notes, then search them")
    kb_sub = kb.add_subparsers(dest="kb_cmd", required=True)
    kbb = kb_sub.add_parser("build", help="index PDFs/notes (default: data/books plus docs/knowledge)")
    kbb.add_argument("paths", nargs="*", help="files or folders to index")
    kbb.add_argument("--no-notes", action="store_true", help="leave out docs/knowledge")
    kbb.add_argument("--index", default="data/kb/index.json")
    kbs = kb_sub.add_parser("search", help="find the passages that best match a question")
    kbs.add_argument("query", nargs="+")
    kbs.add_argument("-k", type=int, default=5, help="number of passages")
    kbs.add_argument("--source", help="only search documents whose title contains this")
    kbs.add_argument("--full", action="store_true", help="print whole passages")
    kbs.add_argument("--index", default="data/kb/index.json")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env(args.env_file)
    handler = {
        "status": cmd_status,
        "download": cmd_download,
        "check-data": cmd_check_data,
        "collect": cmd_collect,
        "stream": cmd_stream,
        "paper-smoke": cmd_paper_smoke,
        "signals": cmd_signals,
        "strategies": cmd_strategies,
        "backtest": cmd_backtest,
        "calendar": cmd_calendar,
        "kb": cmd_kb,
    }[args.cmd]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
