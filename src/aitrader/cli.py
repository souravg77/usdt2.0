"""Command line entry point: ``aitrader <command>``. Run ``aitrader --help`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import json
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

    def run_tests(entries) -> None:
        store = CandleStore(s.db_path)
        for e in entries:
            res = evaluate(e, store, exchanges, symbols, cfg)
            m = res["out_of_sample"]
            verdict = {"paper": "PASSED -> paper trading", "rejected": "rejected"}.get(e.status, "untested")
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
    if cmd == "test":
        if args.ids:
            entries = [reg.get(i) for i in args.ids]
        else:
            entries = reg.with_status("candidate", *(("rejected",) if args.retest else ()))
        if not entries:
            print("nothing to test (add strategies with `learn read` or `learn add-builtin`)")
            return 0
        run_tests(entries)
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

        from .ai import LiveFilter

        store = CandleStore(s.db_path)
        trader = PaperTrader(store, args.registry, exchanges, symbols,
                             PaperConfig(start_equity=s.paper_start_usdt, backtest=cfg),
                             calendar=None if args.no_calendar else CalendarFilter(),
                             ai_filter=None if args.no_ai else LiveFilter(args.models))
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
    import shutil

    os.makedirs(args.dest, exist_ok=True)
    store = CandleStore(s.db_path)
    try:
        store.backup(os.path.join(args.dest, os.path.basename(s.db_path)))
    finally:
        store.close()
    for path in ("data/learn/registry.json", "data/learn/paper_state.json", "data/ai/models/registry.json"):
        if os.path.exists(path):
            target = os.path.join(args.dest, path.replace("/", "__"))
            shutil.copy2(path, target)
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
                    e("AITRADER_WEB_SECRET", ""))
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    secure = e("AITRADER_WEB_SECURE_COOKIE", "false").strip().lower() in ("1", "true", "yes")
    app = create_app(s, auth, JobRunner(env_file=args.env_file), secure_cookie=secure)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, server_header=False)
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
    lt = le_sub.add_parser("test", help="backtest candidates; passes go to paper trading")
    lt.add_argument("ids", nargs="*")
    lt.add_argument("--retest", action="store_true", help="also re-test rejected strategies")
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
    lp.add_argument("--no-ai", action="store_true", help="ignore the active AI trade filter")
    lp.add_argument("--models", default="data/ai/models", help="trade-filter model registry folder")

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
    asc.add_argument("--exchange", dest="score_exchange", default="binance", choices=EXCHANGES)
    asc.add_argument("--symbol", dest="score_symbol", default="BTC/USDT")
    asc.add_argument("--timeframe", default="4h")
    asc.add_argument("--last", type=int, default=20)
    asc.add_argument("--model", help="version (default: the active one)")
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
        "learn": cmd_learn,
        "ai": cmd_ai,
        "web": cmd_web,
        "migrate": cmd_migrate,
        "backup": cmd_backup,
    }[args.cmd]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
