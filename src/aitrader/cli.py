"""Command line entry point: ``aitrader <command>``. Run ``aitrader --help`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from .config import EXCHANGE, Settings
from .data.downloader import download
from .data.feeds import LiveFeed
from .data.quality import check_candles
from .data.store import CandleStore
from .exchanges import PaperExchange, make_adapter
from .models import OrderType, Side
from .safety import live_gate_problems

EXCHANGES = [EXCHANGE]  # Bybit only


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
        for label in ("regime", "setup", "entry", "stop", "target", "filter", "size"):
            if getattr(sp, label):
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


async def cmd_context(s: Settings, args) -> int:
    """Market context (Phase 2): funding, open interest, global market cap; see docs/market-context.md."""
    import time

    from .data.context import ContextStore, collect_context
    from .strategies.context_filter import ContextFilter

    ctx = ContextStore(s.db_path)
    try:
        if args.context_cmd == "collect":
            log = logging.getLogger("aitrader.context")
            while True:
                added = await collect_context(ctx, args.symbol or s.symbols, backfill_days=args.backfill_days)
                log.info("context +%s", ", ".join(f"{m} {n}" for m, n in added.items() if n) or "nothing new")
                if args.once:
                    return 0
                await asyncio.sleep(args.interval)
        now, symbol = int(time.time() * 1000), (args.symbol or ["BTC/USDT"])[0]
        for ex, sym in dict.fromkeys([(args.exchange, symbol), ("global", "*")]):
            for metric in ("funding_rate", "open_interest", "long_short_ratio", "btc_dominance",
                           "total_mcap", "total_mcap_change_24h", "stable_mcap"):
                row = ctx.value_at(metric, ex, sym)
                if row:
                    print(f"{sym:10} {metric:22} {row[1]:>18.6g}  at {_fmt(row[0])} UTC "
                          f"({(now - row[0]) / 3_600_000:.1f}h ago)")
        d = ContextFilter(ctx).decide(args.exchange, symbol, now)
        print(f"now: entries {'allowed' if d.allow_entries else 'BLOCKED'}, size x{d.size_multiplier:g}")
        for r in d.reasons:
            print(f"  - {r}")
        return 0
    finally:
        ctx.close()


def cmd_news(s: Settings, args) -> int:
    """News and web search: free feeds plus Claude's web-search brief (see aitrader.news)."""
    import time

    from .news import NewsFilter, load_latest, update
    from .ops import EventLog
    from .ops.secrets import apply_to_env

    symbols = args.symbol or s.symbols
    if args.news_cmd in ("update", "run"):
        from .news.service import x_reader

        events = EventLog(service="news")
        hours = args.every_hours or float(os.environ.get("AITRADER_NEWS_EVERY_HOURS", "4") or 4)
        x_every = 60 * float(os.environ.get("AITRADER_X_EVERY_MIN", "15") or 15)
        last_brief = 0.0
        while True:
            apply_to_env()  # a key saved on the Settings page is used from the next run on
            due = time.time() - last_brief >= max(0.5, hours) * 3600
            if not due and args.news_cmd == "run":
                # between briefs: check the watched X accounts; a new post about one of our coins
                # brings the next brief forward (at most one extra brief an hour)
                x = x_reader()
                new = x.check_watch(symbols) if x else []
                hot = [p for p in new if p["pairs"]]
                if hot:
                    events.emit("news", f"X: @{hot[0]['username']} posted about {', '.join(hot[0]['pairs'])}: "
                                        f"{hot[0]['text'][:200]}", url=hot[0]["url"])
                    due = time.time() - last_brief >= 3600
            if due:
                out = update(symbols, events=events, max_searches=args.max_searches)
                last_brief = time.time()
                b = out["brief"]
                print(f"news: market risk {b['market_risk']}, {b['searches']} web searches" if b
                      else f"news: feeds saved, no AI brief ({out['brief_error']})", flush=True)
                if args.news_cmd == "update":
                    return 0 if b else 1
            time.sleep(x_every)
    latest = load_latest()
    if not latest:
        print("no news yet: run `aitrader news update`")
        return 1
    b = latest.get("brief")
    if not b:
        print(f"no AI brief: {latest.get('brief_error')}")
    else:
        print(f"market risk {b['market_risk']}: {b['market_risk_why']}\n{b['summary']}")
        for p in b["pairs"]:
            print(f"  {p['symbol']:10} risk {p['risk']:8} demand {p['demand']:7} sentiment {p['sentiment']:+.2f}  {p['why']}")
    for sym in symbols:
        d = NewsFilter().decide(sym)
        print(f"{sym}: new buys {'allowed' if d.allow_entries else 'PAUSED'}, size x{d.size_multiplier:g}")
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


def cmd_learn(s: Settings, args) -> int:
    """The agent's own strategy pipeline: read a book, backtest the drafts, paper-trade survivors."""
    from .backtest import BacktestConfig
    from .learn import (Entry, Registry, RuleError, claude_caller, evaluate, extract_strategies,
                        spec_id, PaperConfig, PaperTrader)
    from .strategies import STRATEGIES

    reg = Registry(args.registry)
    exchanges = args.exchange or EXCHANGES
    symbols = args.symbol or s.symbols
    cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                         slippage_bps=s.paper_slippage_bps)

    def run_tests(entries, move: bool = True) -> None:
        store = CandleStore(s.db_path)
        for e in entries:
            res = evaluate(e, store, exchanges, symbols, cfg, move=move)
            m = res["out_of_sample"]
            if move:
                verdict = {"paper": "PASSED -> paper trading", "rejected": "rejected"}.get(e.status, "untested")
            else:
                verdict = "no data" if not res["markets"] else "failed" if res["failed"] else "passed"
            print(f"  {e.id:40} {verdict:24} oos trades={m['trades']:4d} exp={m['expectancy_r']:+.3f}R"
                  + (f"\n      why: {'; '.join(res['failed'])}" if res["failed"] else ""))
        store.close()
        reg.save()

    cmd = args.learn_cmd
    if cmd == "read":
        try:
            caller = claude_caller()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"reading {args.book} ...")
        ex = extract_strategies(args.book, caller, max_strategies=args.max)
        new = []
        for spec in ex.specs:
            e = Entry(id=spec_id(spec), kind="rule", name=spec["name"], spec=spec,
                      source=f"{spec['source']['book']} {spec['source']['pages']}".strip())
            if reg.add(e):
                new.append(e)
            print(f"  + {e.id:40} {e.source}\n      {spec['description'][:150]}")
        for name, err in ex.rejected:
            print(f"  - {name}: could not be written as rules ({err[:200]})")
        for note in ex.notes:
            print(f"  note {note[:300]}")
        reg.save()
        print(f"{len(new)} new strategies from \"{ex.book}\" -> {args.registry}")
        if args.test and new:
            print("backtesting them:")
            run_tests(new)
        return 0
    if cmd == "add-library":
        from .learn.library import library_specs

        specs, skipped = library_specs(args.book)
        added = 0
        for spec in specs:
            e = Entry(id=spec_id(spec), kind="rule", name=spec["name"], spec=spec,
                      source=f"{spec['source']['book']} {spec['source']['pages']}".strip())
            added += reg.add(e)
        for name, why in skipped:
            print(f"  - {name}: skipped ({why[:150]})")
        reg.save()
        print(f"{added} new strategies from the book library ({len(specs)} in it) -> {args.registry}")
        if args.test:
            run_tests(reg.with_status("candidate"))
        return 0
    if cmd == "add-builtin":
        for name in args.names or list(STRATEGIES):
            e = Entry(id=f"builtin-{name}", kind="builtin", name=name, source=STRATEGIES[name].spec.source)
            print(("  + " if reg.add(e) else "  = ") + e.id)
        reg.save()
        return 0
    if cmd == "paper-all":
        moved = 0
        for e in reg.with_status("candidate", "rejected"):
            e.set_status("paper", "paper-test every strategy (turned on from the dashboard or `learn paper-all`)")
            moved += 1
        reg.save()
        n = len(reg.with_status("paper"))
        print(f"{moved} strategies moved to paper trading; {n} paper trading now "
              f"({len(reg.with_status('retired'))} retired after poor paper results stay retired)")
        return 0
    if cmd == "test":
        if args.ids:
            entries = [reg.get(i) for i in args.ids]
        elif args.paper:
            entries = reg.with_status("paper")
        else:
            entries = reg.with_status("candidate", *(("rejected",) if args.retest else ()))
        if not entries:
            print("nothing to test (add strategies with `learn read` or `learn add-builtin`)")
            return 0
        run_tests(entries, move=not args.paper)
        return 0
    if cmd == "report":
        from .learn.report import backtest_report, known_entries, report_text, save_report

        store = CandleStore(s.db_path)
        try:
            rep = backtest_report(known_entries(reg.entries, builtins=not args.no_builtins), store, args.exchange or [EXCHANGE], symbols,
                                  years=args.years, cfg=cfg,
                                  progress=lambda n, total, sid, k: print(f"  [{n}/{total}] {sid}: {k} trades",
                                                                          flush=True))
        finally:
            store.close()
        path = save_report(rep, args.out)
        print(report_text(rep))
        print(f"report saved to {path}")
        return 0
    if cmd == "list":
        if not reg.entries:
            print("no strategies yet")
        for e in sorted(reg.entries.values(), key=lambda e: (e.status, e.id)):
            bt = e.backtest.get("out_of_sample", {})
            pp = e.paper or {}
            print(f"{e.status:9} {e.id:40} bt oos {bt.get('trades', 0):4d} tr {bt.get('expectancy_r', 0):+.3f}R"
                  f" | paper {pp.get('trades', 0):3d} tr {pp.get('expectancy_r', 0):+.3f}R "
                  f"{pp.get('equity_usdt', 0) or 0:,.0f} USDT  {e.source[:40]}")
        return 0
    if cmd == "show":
        e = reg.get(args.id)
        print(json.dumps({"id": e.id, "status": e.status, "source": e.source, "spec": e.spec,
                          "backtest": e.backtest, "paper": e.paper, "history": e.history},
                         indent=2, default=str))
        return 0
    if cmd == "retire":
        e = reg.get(args.id)
        e.set_status("retired", args.reason or "retired by hand")
        reg.save()
        print(f"{e.id} retired")
        return 0
    if cmd == "paper":
        from .events import CalendarFilter
        from .news import NewsFilter

        from .ai import LiveFilter

        from .ops import EventLog
        from .trade import Control

        store = CandleStore(s.db_path)
        context = None
        if not args.no_context:
            from .data.context import ContextStore
            from .strategies.context_filter import ContextFilter

            context = ContextFilter(ContextStore(store))
        trader = PaperTrader(store, args.registry, exchanges, symbols,
                             PaperConfig(start_equity=s.paper_start_usdt, backtest=cfg),
                             calendar=None if args.no_calendar else CalendarFilter(),
                             ai_filter=None if args.no_ai else LiveFilter(args.models),
                             control=Control(os.path.join(TRADE_DIR, "control.json")),
                             context=context, events=EventLog(service="paper"),
                             news=None if args.no_news else NewsFilter())
        try:
            if args.once:
                print(trader.tick())
            else:
                trader.run(args.interval)
        finally:
            store.close()
        return 0
    raise RuleError(f"unknown learn command {cmd}")


def _filter_strategies(reg, use_all: bool) -> list:
    """(id, strategy) pairs the filter learns from: by default the strategies that passed the
    backtest gate (paper, or retired after paper); with ``use_all`` every builtin and rule."""
    from .learn import make_strategy
    from .learn.registry import Entry
    from .strategies import STRATEGIES

    if not use_all:
        entries = reg.with_status("paper", "retired")
    else:
        entries = [e for e in reg.entries.values() if e.kind == "rule"]
        entries += [Entry(id=f"builtin-{n}", kind="builtin", name=n) for n in STRATEGIES]
    return [(e.id, make_strategy(e)) for e in entries]


def cmd_ai(s: Settings, args) -> int:
    """Phase 4 trade filter: build the training set, train and test, promote, roll back."""
    import getpass

    from .ai import ModelRegistry, TrainConfig, build_dataset, load_dataset, save_dataset, train
    from .backtest import BacktestConfig
    from .learn import Registry

    models = ModelRegistry(args.models)
    cmd = args.ai_cmd

    def make_dataset():
        reg = Registry(args.registry)
        strategies = _filter_strategies(reg, args.all)
        if not strategies:
            print("no strategies to learn from: run `aitrader learn add-library --test` (or "
                  "`learn add-builtin` + `learn test`) first, or pass --all", file=sys.stderr)
            return None
        cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                             slippage_bps=s.paper_slippage_bps)
        store = CandleStore(s.db_path)
        try:
            frame = build_dataset(store, strategies, args.exchange or EXCHANGES,
                                  args.symbol or s.symbols, cfg, args.include_final,
                                  None if args.no_paper else args.paper_state)
        finally:
            store.close()
        save_dataset(frame, args.dataset)
        print(f"{len(frame)} signals from {len(strategies)} strategies -> {args.dataset}")
        if len(frame):
            by = frame.groupby("source").size().to_dict()
            print(f"  sources {by}, reached 1R {frame['hit_1r'].mean():.0%}, "
                  f"unfiltered trades {int(frame['taken'].sum())}")
        return frame

    def do_train(frame) -> int:
        current = models.load_active()
        model, report = train(frame, TrainConfig(), current=current[1] if current else None)
        version = models.add(model, report, {"path": args.dataset, "rows": len(frame)})
        print(models.report_text(version))
        if report["passed"]:
            print(f"{version} is a CANDIDATE. It is not in use until you approve it:\n"
                  f"  aitrader ai promote {version}")
        else:
            print(f"{version} was REJECTED and will not be used.")
        return 0

    if cmd == "dataset":
        return 0 if make_dataset() is not None else 1
    if cmd in ("train", "retrain"):
        if cmd == "retrain":
            frame = make_dataset()
            if frame is None:
                return 1
        else:
            if not os.path.exists(args.dataset):
                print(f"no training set at {args.dataset}; run `aitrader ai dataset` first",
                      file=sys.stderr)
                return 1
            frame = load_dataset(args.dataset)
        try:
            return do_train(frame)
        except ValueError as exc:
            print(f"not trained: {exc}", file=sys.stderr)
            return 1
    if cmd == "list":
        if not models.versions():
            print("no models yet (aitrader ai retrain)")
        for v in models.versions():
            m = models.info(v)
            u, f = m["test"]["unfiltered"], m["test"]["filtered"]
            print(f"{v:18} {m['status']:9} test: unfiltered {u['trades']:4d} tr "
                  f"{u['expectancy_r']:+.3f}R dd {u['max_drawdown_r']:.1f}R | filtered "
                  f"{f['trades']:4d} tr {f['expectancy_r']:+.3f}R dd {f['max_drawdown_r']:.1f}R"
                  + ("  <- active" if v == models.active else ""))
        return 0
    if cmd == "report":
        v = args.version or (models.versions() or [None])[-1]
        if not v:
            print("no models yet")
            return 1
        print(models.report_text(v))
        return 0
    if cmd == "promote":
        print(models.report_text(args.version))
        by = args.by or os.environ.get("USER") or getpass.getuser()
        if not args.yes:
            if not sys.stdin.isatty():
                print("promotion needs a person: run it in a terminal, or pass --yes", file=sys.stderr)
                return 1
            typed = input(f"Type the version ({args.version}) to make it the active filter: ").strip()
            if typed != args.version:
                print("not promoted")
                return 1
        try:
            models.promote(args.version, by)
        except (ValueError, KeyError) as exc:
            print(f"not promoted: {exc}", file=sys.stderr)
            return 1
        print(f"{args.version} is now the active trade filter (approved by {by}). "
              "The paper service picks it up on its next tick. Undo: aitrader ai rollback")
        return 0
    if cmd == "rollback":
        prev = models.rollback(getpass.getuser())
        print(f"active trade filter is now {prev or 'off (every signal is taken)'}")
        return 0
    if cmd == "off":
        models.deactivate(getpass.getuser())
        print("trade filter off: every signal is taken at full size")
        return 0
    if cmd == "score":
        from .ai.dataset import signal_rows
        from .indicators import candles_to_frame
        from .learn import make_strategy

        reg = Registry(args.registry)
        entry = reg.get(args.strategy)
        version = args.model or models.active
        if not version:
            print("no active model; pass --model VERSION", file=sys.stderr)
            return 1
        model = models.load(version)
        store = CandleStore(s.db_path)
        candles = store.load(args.score_exchange, args.score_symbol, args.timeframe)
        store.close()
        if not candles:
            print("no stored candles for that market", file=sys.stderr)
            return 1
        from .ai.dataset import finish

        frame = finish(signal_rows(make_strategy(entry), entry.id, candles_to_frame(candles),
                                   args.score_exchange, args.score_symbol, args.timeframe))
        frame = frame.tail(args.last)
        if frame.empty:
            print("no signals")
            return 0
        frame["prob"] = model.predict(frame)
        frame["size"] = model.sizes(frame["prob"])
        for _, r in frame.iterrows():
            verdict = f"take x{r['size']:.2f}" if r["size"] > 0 else "skip"
            print(f"{_fmt(int(r['signal_ts']))}  p={r['prob']:.3f}  {verdict:10} "
                  f"(result {r['r']:+.2f}R, reached 1R: {'yes' if r['hit_1r'] else 'no'})")
        return 0
    raise ValueError(f"unknown ai command {cmd}")


def cmd_migrate(s: Settings, _args) -> int:
    """Bring the database schema up to date (run by deploy/update.sh after each update)."""
    store = CandleStore(s.db_path)
    try:
        applied = store.migrate()
        print(f"database {s.db_path}: schema v{store.schema_version()}"
              + (f" (applied {', '.join(f'v{v}' for v in applied)})" if applied else " (up to date)"))
    finally:
        store.close()
    return 0


def cmd_backup(s: Settings, args) -> int:
    """Copy the database and the strategy/model registries into a folder."""
    import glob
    import shutil

    os.makedirs(args.dest, exist_ok=True)
    store = CandleStore(s.db_path)
    try:
        store.backup(os.path.join(args.dest, os.path.basename(s.db_path)))
    finally:
        store.close()
    for path in ("data/learn/registry.json", "data/learn/paper_state.json", "data/ai/models/registry.json",
                 "data/trade/testnet_state.json", "data/trade/live_state.json", "data/trade/control.json",
                 "data/golive/approval.json", "data/golive/attestations.json", "data/settings/keys.enc"):
        if os.path.exists(path):
            target = os.path.join(args.dest, path.replace("/", "__"))
            shutil.copy2(path, target)
    for path in sorted(glob.glob("data/users/*/*.json")):  # each user's positions and switches
        shutil.copy2(path, os.path.join(args.dest, path.replace("/", "__")))
    if os.path.exists(ACCOUNTS_DB):  # user accounts (keys inside stay encrypted)
        from .accounts import UserStore

        accounts = UserStore(ACCOUNTS_DB)
        try:
            accounts.backup(os.path.join(args.dest, "accounts.db"))
        finally:
            accounts.close()
    print(f"backup written to {args.dest}")
    return 0


def cmd_web(s: Settings, args) -> int:
    """Serve the dashboard (login from AITRADER_WEB_USER / AITRADER_WEB_PASSWORD)."""
    try:
        import uvicorn

        from .web import Auth, AuthError, JobRunner, create_app
    except ImportError as exc:  # pragma: no cover - installed with the package
        print(f"dashboard dependencies missing: {exc}", file=sys.stderr)
        return 1
    e = os.environ.get
    try:
        auth = Auth(e("AITRADER_WEB_USER", "admin"), e("AITRADER_WEB_PASSWORD", ""),
                    e("AITRADER_WEB_SECRET", ""), revoked_path="data/web/revoked.json")
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    secure = e("AITRADER_WEB_SECURE_COOKIE", "false").strip().lower() in ("1", "true", "yes")
    from .accounts import UserStore

    signup = e("AITRADER_WEB_SIGNUP", "closed").strip().lower() in ("open", "true", "1", "yes")
    app = create_app(s, auth, JobRunner(env_file=args.env_file), secure_cookie=secure,
                     users=UserStore(ACCOUNTS_DB), keys_secret=e("AITRADER_KEYS_SECRET", ""), signup=signup)
    # Behind the caddy proxy the client address comes from X-Forwarded-For (Caddy overwrites it),
    # so the login lockout and sign-up limits count each visitor, not the proxy.
    trust = e("AITRADER_WEB_TRUST_PROXY", "false").strip().lower() in ("1", "true", "yes")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=trust,
                forwarded_allow_ips="*" if trust else None, server_header=False)
    return 0



# ---- Phases 5 and 6: executor, kill switch, alerts, go-live ------------------------------------

TRADE_DIR = "data/trade"
PAPER_STATE = "data/learn/paper_state.json"


def _events(service: str):
    from .ops import EventLog

    return EventLog(service=service)


def cmd_trade(s: Settings, args) -> int:
    """Pause, resume, kill switch, status, and the testnet / live-pilot executor."""
    from .trade import Control
    from .trade.report import executor_state_paths, status_text

    control = Control(os.path.join(TRADE_DIR, "control.json"))
    who = args.by or os.environ.get("USER", "cli")
    cmd = args.trade_cmd
    if cmd == "status":
        print(status_text(s, control, PAPER_STATE, executor_state_paths(TRADE_DIR)))
        return 0
    if cmd == "pause":
        control.pause(args.reason or "paused by hand", by=who)
        _events("cli").emit("risk_pause", f"paused by {who}: {args.reason or 'by hand'}", level="warning")
        print("paused: no new entries (open positions are still managed). Undo: aitrader trade resume")
        return 0
    if cmd == "resume":
        control.resume(by=who)
        _events("cli").emit("resume", f"resumed by {who}")
        print("resumed")
        return 0
    if cmd == "kill":
        if not args.confirm:
            print("The kill switch stops all entries and sells every position the executors opened.\n"
                  "Run again with --confirm to pull it.")
            return 1
        control.kill(args.reason or "kill switch", by=who)
        _events("cli").emit("kill", f"kill switch pulled by {who}", level="warning")
        print("kill switch on: executors cancel and sell on their next tick. Undo: aitrader trade resume")
        return 0
    if cmd == "run":
        return asyncio.run(_trade_run(s, args, control))
    raise SystemExit(f"unknown trade command {cmd}")


async def _trade_run(s: Settings, args, control) -> int:
    from .safety import assert_live_allowed
    from .trade import Executor, GateConfig, GoLive, RiskEngine, RiskLimits

    mode = args.mode
    events = _events(f"executor-{mode}")
    if mode == "testnet":
        from .ops.secrets import restart_on_change

        restart_on_change()  # testnet keys saved on the Settings page take effect by themselves
    strategies = set(args.strategy or []) or None
    guard = monitor = pilot = None
    if mode == "live":
        assert_live_allowed(s)  # the three live-lock lines in .env, opened by you
        pilot = float(os.environ.get("AITRADER_PILOT_MAX_USDT", "0") or 0)
        if pilot <= 0:
            print("set AITRADER_PILOT_MAX_USDT in .env: the most USDT the live pilot may use", file=sys.stderr)
            return 1
        gl = GoLive(events=events, cfg=GateConfig.from_env())
        approved = sorted((gl._load("approval.json").get("strategies") or {}))
        if not approved:
            print("no go-live approval: run `aitrader golive check` then `aitrader golive approve`",
                  file=sys.stderr)
            return 1
        strategies = (strategies or set(approved)) & set(approved)
        if not strategies or len(strategies) > 2:
            print("the live pilot runs 1 or 2 approved strategies", file=sys.stderr)
            return 1
        guard, monitor = gl.guard, gl.monitor
    elif s.mode.value != "testnet":
        print("testnet runs need AITRADER_MODE=testnet (the compose executor-testnet service sets it)",
              file=sys.stderr)
        return 1
    if mode == "testnet":
        keys = s.keys.get(f"{args.exchange}_demo")
        if not (keys and keys.api_key and keys.secret):
            msg = ("demo executor: waiting for the Bybit Demo Trading API key and secret "
                   "(add them on the dashboard's Settings page)")
            events.emit("waiting", msg, level="warning")
            print(msg, file=sys.stderr)
            if args.once:
                return 1
            while True:  # don't fail every tick: the keys watcher restarts us once they are saved
                await asyncio.sleep(3600)
    adapter = make_adapter(args.exchange, s, testnet=(mode == "testnet"))
    ex = Executor(adapter, mode, args.exchange, PAPER_STATE,
                  state_path=os.path.join(TRADE_DIR, f"{mode}_state.json"), strategies=strategies,
                  control=control, risk=RiskEngine(RiskLimits.from_env()), events=events,
                  live_guard=guard, monitor=monitor, pilot_max_usdt=pilot)
    try:
        if args.once:
            print(json.dumps(await ex.tick(), indent=1))
        else:
            await ex.run(args.interval)
    finally:
        await adapter.close()
    return 0


def cmd_golive(s: Settings, args) -> int:
    """Phase 6 go/no-go gate: check it, record the manual items, approve or revoke."""
    from .trade import GateConfig, GoLive

    gl = GoLive(events=_events("cli"), cfg=GateConfig.from_env())
    cmd = args.golive_cmd
    if cmd == "check":
        rep = gl.evaluate(args.strategy or None)
        for c in rep["criteria"]:
            print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['value']}  (needs {c['threshold']})")
        print("GATE PASSES: you may approve (aitrader golive approve)" if rep["passed"] else "gate does not pass yet")
        print(f"live lock in .env: {'open' if not live_gate_problems(s) else 'locked'}")
        return 0 if rep["passed"] else 1
    if cmd == "attest":
        a = gl.attest(args.item, by=args.by, note=args.note or "")
        print(f"recorded: {args.item} confirmed by {a['by']}")
        return 0
    if cmd == "approve":
        try:
            gl.approve(args.strategy or [], by=args.by)
        except ValueError as exc:
            print(f"not approved: {exc}", file=sys.stderr)
            return 1
        print("approved. Live orders still need the live lock in .env opened by you, "
              "AITRADER_PILOT_MAX_USDT, and the executor-live service started.")
        return 0
    if cmd == "revoke":
        gl.revoke(by=args.by or "")
        print("approval revoked: the live executor stops opening trades")
        return 0
    raise SystemExit(f"unknown golive command {cmd}")


def cmd_telegram(s: Settings, args) -> int:
    """Service: send alerts and summaries to Telegram and take /status, /pause, /resume, /kill."""
    from .ops.telegram import Telegram, TelegramService
    from .trade import Control
    from .trade.report import daily_text, executor_state_paths, status_text

    from .ops.secrets import restart_on_change

    e = os.environ.get
    restart_on_change()  # a token saved on the Settings page takes effect without a manual restart
    try:
        bot = Telegram(e("TELEGRAM_BOT_TOKEN", ""), e("TELEGRAM_CHAT_ID", ""))
    except ValueError as exc:
        print(f"{exc} (or on the dashboard's Settings page); waiting for them", file=sys.stderr)
        while True:  # don't crash-loop: the keys watcher restarts us once they are saved
            time.sleep(3600)
    control = Control(os.path.join(TRADE_DIR, "control.json"))
    paths = executor_state_paths(TRADE_DIR)
    svc = TelegramService(bot, _events("telegram"), control,
                          status_fn=lambda: status_text(s, control, PAPER_STATE, paths),
                          summary_fn=lambda day: daily_text(day, PAPER_STATE, paths))
    if args.test:
        bot.send("aitrader: test message. Alerts will arrive here.")
        print("sent")
        return 0
    svc.run()
    return 0


def cmd_report(s: Settings, args) -> int:
    """Weekly paper-vs-backtest report (also sent to Telegram through the event log)."""
    from .trade.report import executor_state_paths, weekly, weekly_text

    rep = weekly(args.registry, PAPER_STATE, executor_state_paths(TRADE_DIR), max_gap_r=args.max_gap)
    text = weekly_text(rep)
    print(text)
    os.makedirs("data/reports", exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(f"data/reports/weekly-{day}.json", "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    if not args.quiet:
        _events("report").emit("weekly_report", text, level="warning" if rep["flagged"] else "info")
    return 0



ACCOUNTS_DB = "data/accounts.db"


def cmd_users(s: Settings, args) -> int:
    """User accounts: list, approve, disable, allow live, and the per-user trading service."""
    from .accounts import AccountError, UserStore

    store = UserStore(ACCOUNTS_DB)
    try:
        cmd = args.users_cmd
        if cmd == "list":
            for u in store.list():
                keys = ", ".join(f'{k["exchange"]}{"-testnet" if k["testnet"] else ""}' for k in store.key_info(u.id))
                print(f"{u.id:4} {u.email:40} {u.status:9} {u.mode:8} live {'allowed' if u.live_allowed else 'off':8}"
                      f" keys: {keys or '-'}")
            return 0
        if cmd in ("approve", "disable", "live-on", "live-off"):
            u = store.by_email(args.email)
            if u is None:
                print(f"no account {args.email}", file=sys.stderr)
                return 1
            if cmd == "approve":
                store.set_status(u.id, "active")
            elif cmd == "disable":
                store.set_status(u.id, "disabled")
            else:
                from .accounts.live import turn_off

                if cmd == "live-off":
                    turn_off(store, "data/users", u.id, actor="admin (cli)", reason="stopped by the admin")
                store.allow_live(u.id, cmd == "live-on")
            if cmd == "disable":
                from .accounts.live import turn_off

                turn_off(store, "data/users", u.id, actor="admin (cli)", reason="account disabled")
            store.log(u.id, "admin (cli)", {"approve": "active", "disable": "disabled", "live-on": "live_allowed",
                                            "live-off": "live_not_allowed"}[cmd])
            print(f"{u.email}: {cmd} done")
            return 0
        if cmd == "add":
            import getpass

            pw = getpass.getpass("password for the new account: ")
            try:
                u = store.create(args.email, pw, status="active")
            except AccountError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"created {u.email} (active)")
            return 0
        if cmd == "run":
            return asyncio.run(_users_run(s, store, args))
        raise SystemExit(f"unknown users command {cmd}")
    finally:
        store.close()


async def _users_run(s: Settings, store, args) -> int:
    from .accounts import AccountError, Vault
    from .accounts.runner import UsersRunner
    from .trade import Control, GateConfig, GoLive

    try:
        vault = Vault(os.environ.get("AITRADER_KEYS_SECRET", ""))
    except AccountError as exc:
        print(exc, file=sys.stderr)
        return 1
    gl = GoLive(events=_events("users"), cfg=GateConfig.from_env())
    runner = UsersRunner(store, vault, s, PAPER_STATE, platform_control=Control(os.path.join(TRADE_DIR, "control.json")),
                         live_guard=gl.guard, live_monitor=gl.monitor)
    if args.once:
        print(json.dumps(await runner.tick(), indent=1, default=str))
    else:
        await runner.run(args.interval)
    return 0



def cmd_doctor(s: Settings, args) -> int:
    """Health check of this installation: settings, secrets, live lock, data, services, disk, backups."""
    from .ops.doctor import report, run_checks

    text, code = report(run_checks(s, env_file=args.env_file))
    print(text)
    return code

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aitrader", description=__doc__)
    p.add_argument("--env-file", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show mode, live-gate state and which keys are set")
    sub.add_parser("doctor", help="health check: settings, secrets, live lock, data, services, disk, backups")

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
    st.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    st.add_argument("--symbol", action="append")
    st.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")

    ps = sub.add_parser("paper-smoke", help="open and close a test position on the paper exchange")
    ps.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    ps.add_argument("--symbol", default="BTC/USDT")
    ps.add_argument("--usdt", type=float, default=100.0)

    sg = sub.add_parser("signals", help="run a strategy over stored candles and save its signal log")
    sg.add_argument("--strategy", required=True)
    sg.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    sg.add_argument("--symbol", default="BTC/USDT")
    sg.add_argument("--timeframe", default="4h")
    sg.add_argument("--since", help="UTC date")
    sg.add_argument("--out", help="CSV path")
    sg.add_argument("--show", type=int, default=10, help="print the last N signals")

    sub.add_parser("strategies", help="print every strategy's rule card")

    bt = sub.add_parser("backtest", help="backtest a strategy on stored candles (fees + slippage)")
    bt.add_argument("--strategy", required=True)
    bt.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    bt.add_argument("--symbol", action="append")
    bt.add_argument("--timeframe", default="4h")
    bt.add_argument("--taker-fee", type=float, default=0.001)
    bt.add_argument("--maker-fee", type=float, default=0.001)
    bt.add_argument("--slippage-bps", type=float, default=5.0)
    bt.add_argument("--funding-8h", type=float, default=0.0, help="perps funding per 8h (0 for spot)")
    bt.add_argument("--months", type=int, default=12, help="months of the results calendar to print")
    bt.add_argument("--final", action="store_true", help="also run the untouched final test period")

    nw = sub.add_parser("news", help="world and crypto news: feeds plus the AI's web-search brief")
    nw.add_argument("news_cmd", choices=["update", "run", "show"])
    nw.add_argument("--symbol", action="append")
    nw.add_argument("--every-hours", type=float, default=None,
                    help="for run: hours between briefs (default AITRADER_NEWS_EVERY_HOURS or 4)")
    nw.add_argument("--max-searches", type=int, default=None, help="web searches per brief (default 6)")

    ca = sub.add_parser("calendar", help="show upcoming high-impact events and the current decision")
    ca.add_argument("--hours", type=float, default=72)

    cx = sub.add_parser("context", help="market context: funding, open interest, total market cap")
    cx.add_argument("context_cmd", choices=["collect", "show"])
    cx.add_argument("--symbol", action="append")
    cx.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES, help="for show")
    cx.add_argument("--once", action="store_true", help="collect: one top-up, then exit")
    cx.add_argument("--interval", type=float, default=900, help="collect: seconds between top-ups")
    cx.add_argument("--backfill-days", type=int, default=30, help="collect: history on first run")

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

    le = sub.add_parser("learn", help="agent-written strategies: read a book, backtest, paper-trade")
    le.add_argument("--registry", default="data/learn/registry.json")
    le.add_argument("--exchange", action="append", choices=EXCHANGES)
    le.add_argument("--symbol", action="append")
    le_sub = le.add_subparsers(dest="learn_cmd", required=True)
    lr = le_sub.add_parser("read", help="have Claude read a book and draft strategies (needs ANTHROPIC_API_KEY)")
    lr.add_argument("book", help="PDF, Markdown or text file")
    lr.add_argument("--max", type=int, default=12, help="most strategies to keep from one book")
    lr.add_argument("--test", action="store_true", help="backtest the new drafts straight away")
    ll = le_sub.add_parser("add-library", help="register the strategies already read from the supplied books")
    ll.add_argument("--book", help="only books whose title contains this")
    ll.add_argument("--test", action="store_true", help="backtest them straight away")
    lb = le_sub.add_parser("add-builtin", help="register the hand-coded strategies too")
    lb.add_argument("names", nargs="*")
    le_sub.add_parser("paper-all", help="paper-trade every strategy, including untested and rejected "
                                        "ones (fake money; poor ones retire on their paper results)")
    lt = le_sub.add_parser("test", help="backtest candidates; passes go to paper trading")
    lt.add_argument("ids", nargs="*")
    lt.add_argument("--retest", action="store_true", help="also re-test rejected strategies")
    lt.add_argument("--paper", action="store_true", help="backtest the strategies already paper trading and "
                                                        "record the result, without moving them")
    lrp = le_sub.add_parser("report", help="backtest every strategy over the last N years: win report "
                                           "per strategy and per year")
    lrp.add_argument("--years", type=int, default=5)
    lrp.add_argument("--out", default="data/reports/backtest.json")
    lrp.add_argument("--no-builtins", action="store_true", help="leave out the hand-coded strategies")
    le_sub.add_parser("list", help="every strategy with its status and results")
    ls = le_sub.add_parser("show", help="full rules, sources and results of one strategy")
    ls.add_argument("id")
    lx = le_sub.add_parser("retire", help="stop paper-trading a strategy")
    lx.add_argument("id")
    lx.add_argument("--reason")
    lp = le_sub.add_parser("paper", help="service: paper-trade every strategy that passed")
    lp.add_argument("--interval", type=float, default=60)
    lp.add_argument("--once", action="store_true")
    lp.add_argument("--no-calendar", action="store_true", help="ignore the economic calendar filter")
    lp.add_argument("--no-news", action="store_true", help="ignore the AI news filter")
    lp.add_argument("--no-ai", action="store_true", help="ignore the active AI trade filter")
    lp.add_argument("--models", default="data/ai/models", help="trade-filter model registry folder")
    lp.add_argument("--no-context", action="store_true", help="ignore the market-context filter")

    sub.add_parser("migrate", help="bring the database schema up to date")
    bk = sub.add_parser("backup", help="copy the database and registries into a folder")
    bk.add_argument("dest")

    w = sub.add_parser("web", help="dashboard: submit books, see strategies and results (login required)")
    w.add_argument("--host", default="127.0.0.1", help="0.0.0.0 only inside Docker or behind a proxy")
    w.add_argument("--port", type=int, default=8080)

    ai = sub.add_parser("ai", help="Phase 4 AI trade filter: training set, train/test, promote, rollback")
    ai.add_argument("--models", default="data/ai/models", help="model registry folder")
    ai.add_argument("--dataset", default="data/ai/dataset.csv")
    ai.add_argument("--registry", default="data/learn/registry.json", help="strategy registry")
    ai_sub = ai.add_subparsers(dest="ai_cmd", required=True)
    for name, text in [("dataset", "backtest the strategies and save one row per signal"),
                       ("retrain", "build the training set, then train and test a new model "
                                   "(never promotes; run monthly)")]:
        a = ai_sub.add_parser(name, help=text)
        a.add_argument("--exchange", action="append", choices=EXCHANGES)
        a.add_argument("--symbol", action="append")
        a.add_argument("--all", action="store_true",
                       help="learn from every strategy, not just those that passed `learn test`")
        a.add_argument("--include-final", action="store_true",
                       help="also use the final 20%% of history kept back for the go/no-go test")
        a.add_argument("--no-paper", action="store_true", help="leave out closed paper trades")
        a.add_argument("--paper-state", default="data/learn/paper_state.json")
    ai_sub.add_parser("train", help="train and test a new model on the saved training set")
    ai_sub.add_parser("list", help="every model version with its test results")
    ar = ai_sub.add_parser("report", help="side-by-side report of one version (default: newest)")
    ar.add_argument("version", nargs="?")
    ap = ai_sub.add_parser("promote", help="make a candidate the active filter (needs your approval)")
    ap.add_argument("version")
    ap.add_argument("--by", help="who approves (default: your user name)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    ai_sub.add_parser("rollback", help="switch back to the previous model at once")
    ai_sub.add_parser("off", help="stop filtering: take every signal at full size")
    asc = ai_sub.add_parser("score", help="show the filter's probability for a strategy's recent signals")
    asc.add_argument("--strategy", required=True, help="strategy id or name from `learn list`")
    asc.add_argument("--exchange", dest="score_exchange", default=EXCHANGE, choices=EXCHANGES)
    asc.add_argument("--symbol", dest="score_symbol", default="BTC/USDT")
    asc.add_argument("--timeframe", default="4h")
    asc.add_argument("--last", type=int, default=20)
    asc.add_argument("--model", help="version (default: the active one)")

    tr = sub.add_parser("trade", help="Phases 5-6: pause / resume / kill switch, and the order executor")
    tr_sub = tr.add_subparsers(dest="trade_cmd", required=True)
    tr_sub.add_parser("status", help="mode, live lock, pause state, positions, today's results")
    for name, h in (("pause", "stop new entries everywhere"), ("resume", "allow entries again"),
                    ("kill", "kill switch: stop entries and sell everything the executors bought")):
        t = tr_sub.add_parser(name, help=h)
        t.add_argument("--reason")
        t.add_argument("--by", help="who (for the record)")
        if name == "kill":
            t.add_argument("--confirm", action="store_true")
    tr.set_defaults(by=None, reason=None, confirm=False)
    trr = tr_sub.add_parser("run", help="service: mirror the paper trades with real orders")
    trr.add_argument("--mode", choices=["testnet", "live"], default="testnet")
    trr.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    trr.add_argument("--strategy", action="append", help="only these registry ids (repeatable)")
    trr.add_argument("--interval", type=float, default=15)
    trr.add_argument("--once", action="store_true", help="one pass, then exit")

    gl = sub.add_parser("golive", help="Phase 6 go/no-go gate: check, attest, approve, revoke")
    gl_sub = gl.add_subparsers(dest="golive_cmd", required=True)
    glc = gl_sub.add_parser("check", help="evaluate every gate criterion")
    glc.add_argument("--strategy", action="append")
    gla = gl_sub.add_parser("attest", help="confirm a manual item: keys, kill_switch or compliance")
    gla.add_argument("item", choices=["keys", "kill_switch", "compliance"])
    gla.add_argument("--by", required=True)
    gla.add_argument("--note")
    glp = gl_sub.add_parser("approve", help="approve 1-2 strategies for the live pilot (gate must pass)")
    glp.add_argument("--strategy", action="append", required=True)
    glp.add_argument("--by", required=True)
    glr = gl_sub.add_parser("revoke", help="withdraw the approval: live goes back to paper")
    glr.add_argument("--by")

    tg = sub.add_parser("telegram", help="service: Telegram alerts and /status /pause /resume /kill")
    tg.add_argument("--test", action="store_true", help="send one test message and exit")

    rp = sub.add_parser("report", help="weekly paper-vs-backtest report")
    rp.add_argument("report_cmd", choices=["weekly"])
    rp.add_argument("--registry", default="data/learn/registry.json")
    rp.add_argument("--max-gap", type=float, default=0.15)
    rp.add_argument("--quiet", action="store_true", help="don't send it to the event log / Telegram")

    us = sub.add_parser("users", help="user accounts: list, approve, disable, live on/off, run")
    us_sub = us.add_subparsers(dest="users_cmd", required=True)
    us_sub.add_parser("list", help="every account with its status, mode and connected keys")
    for name, h in (("approve", "let a new account connect keys and trade on the testnet"),
                    ("disable", "block an account: sells what the agent bought for it and stops it"),
                    ("live-on", "allow an account to switch itself to real money"),
                    ("live-off", "stop real money for an account (back to the testnet)"),
                    ("add", "create an active account (asks for the password)")):
        u = us_sub.add_parser(name, help=h)
        u.add_argument("email")
    ur = us_sub.add_parser("run", help="service: trade for every account on its own exchange account")
    ur.add_argument("--interval", type=float, default=15)
    ur.add_argument("--once", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    from .ops.secrets import apply_to_env

    apply_to_env()  # keys saved on the dashboard's Settings page
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
        "context": cmd_context,
        "news": cmd_news,
        "kb": cmd_kb,
        "learn": cmd_learn,
        "ai": cmd_ai,
        "web": cmd_web,
        "migrate": cmd_migrate,
        "backup": cmd_backup,
        "trade": cmd_trade,
        "golive": cmd_golive,
        "telegram": cmd_telegram,
        "report": cmd_report,
        "users": cmd_users,
        "doctor": cmd_doctor,
    }[args.cmd]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
