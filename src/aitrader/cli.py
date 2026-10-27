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
from .data.downloader import closed_since, deepen, download, top_up
from .data.feeds import LiveFeed
from .data.quality import check_candles
from .data.store import CandleStore
from .exchanges import PaperExchange, make_adapter
from .market.pairs import configured_symbols, open_paper_symbols
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
    print(f"symbols: {', '.join(configured_symbols(s.symbols))} "
          f"(AITRADER_PAIRS={os.environ.get('AITRADER_PAIRS') or 'fixed'})")
    print(f"timeframes: {', '.join(s.timeframes)}")
    return 0


async def cmd_download(s: Settings, args) -> int:
    from .data.perp import PerpCandles, db_for

    mkt = getattr(args, "market", "spot") or "spot"
    store = CandleStore(db_for(s.db_path, mkt))  # Futures AI: perpetual candles in its own database
    since = _ms(args.since)
    until = _ms(args.until) if args.until else None
    exchanges = args.exchange or EXCHANGES
    for ex in exchanges:
        adapter = make_adapter(ex, s)
        if mkt == "futures":
            adapter = PerpCandles(adapter)
        try:
            for sym in args.symbol or configured_symbols(s.symbols, market=mkt):
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


CLOSE_LAG_MS = 3_000  # Bybit serves a closed candle a moment after its close


async def cmd_collect(s: Settings, args) -> int:
    """Long-running service: keep the candle database topped up from both exchanges."""
    import time

    log = logging.getLogger("aitrader.collect")
    from .data.perp import PerpCandles, futures_db_path
    from .learn import wallet
    from .learn.paper import history_start

    store = CandleStore(s.db_path)
    fstore = CandleStore(futures_db_path(s.db_path))  # Futures AI's perpetual candles, apart from spot
    exchanges = args.exchange or EXCHANGES
    adapters = {ex: make_adapter(ex, s) for ex in exchanges}
    backlog: dict[str, int] = {}  # where the history filling stopped last pass, per exchange
    auto_path = wallet.autoscan_path(PAPER_STATE)
    fast: list[tuple] = []  # charts with candles, for Auto scan's quick fetches between passes
    fast_at = [0]  # when their candles were last fetched (ms, less the closing lag)

    async def quick() -> None:
        """Auto scan on: fetch the just-closed candle of each chart whose candle closed since the last
        fetch (on 1m charts each minute, on 4h charts every 4 hours), so paper trading sees it in seconds."""
        if not fast or not 0 < wallet.autoscan(auto_path) < wallet.FULL_SCAN_SECONDS:
            return
        now = int(time.time() * 1000) - CLOSE_LAG_MS
        due = [c for c in fast if closed_since(c[5], fast_at[0], now)]
        fast_at[0] = now
        for ex, mkt, db, source, sym, tf in due:
            try:
                n = await top_up(source, db, sym, tf)
                if n:
                    log.info("%s %s %s %s +%d (auto scan)", ex, mkt, sym, tf, n)
            except Exception as exc:
                log.warning("%s %s %s %s failed: %s", ex, mkt, sym, tf, exc)

    try:
        while True:
            since = int(time.time() * 1000) - args.backfill_days * 86_400_000
            # the ranked pairs when AITRADER_PAIRS=ranked, plus any with an open paper position
            from .market import gainers, perp_limits

            for gm in gainers.MARKETS:  # each scanner switched on: its list, about once an hour
                gpath = gainers.path_for(gm)
                if not gainers.due(gpath):
                    continue
                client = gainers.client_for(gm)
                try:
                    await gainers.refresh(client, gpath, market=gm, limits_path=perp_limits.DEFAULT_PATH)
                    log.info("%s top gainers: %s", gm, ", ".join(gainers.symbols(gpath)) or "none")
                except Exception as exc:
                    log.warning("%s top gainers refresh failed: %s", gm, exc)
                finally:
                    await client.close()
            from .market import squeeze

            if squeeze.due(squeeze.path_from_env()):  # the squeeze scanner on: its list, about once an hour
                client = gainers.perp_client()
                try:
                    await squeeze.refresh(client, squeeze.path_from_env())
                    log.info("squeeze pairs: %s", ", ".join(squeeze.symbols(squeeze.path_from_env())) or "none")
                except Exception as exc:
                    log.warning("squeeze scan failed: %s", exc)
                finally:
                    await client.close()
            if wallet.engines(wallet.engines_path(PAPER_STATE))["futures"] and perp_limits.due():
                client = gainers.perp_client()  # Futures AI on: each coin's maximum leverage, once a day
                try:
                    await perp_limits.refresh(client)
                except Exception as exc:
                    log.warning("perpetual leverage limits refresh failed: %s", exc)
                finally:
                    await client.close()
            # each engine its own market: Spot AI's pairs from spot, Futures AI's from the perpetuals
            jobs = [("spot", store, configured_symbols(s.symbols, market="spot",
                                                       keep=open_paper_symbols(PAPER_STATE, "spot"))),
                    ("futures", fstore, configured_symbols(s.symbols, market="futures",
                                                           keep=open_paper_symbols(PAPER_STATE, "futures")))]
            fast_new: list[tuple] = []
            for ex, adapter in adapters.items():
                began = time.monotonic()
                began_ms = int(time.time() * 1000) - CLOSE_LAG_MS
                # first every chart's newest candles (one request each), so paper trading reads a
                # just-closed candle within a minute or so; the slower history and hole filling
                # comes after, within a time budget, carrying on next pass where it stopped
                todo, empty = [], []  # empty: charts with nothing stored yet (a new pair), filled first
                for mkt, db, symbols in jobs:
                    source = adapter if mkt == "spot" else PerpCandles(adapter)
                    for sym in symbols:
                        for tf in s.timeframes:
                            n = 0
                            try:
                                n = await top_up(source, db, sym, tf)
                                if n:
                                    log.info("%s %s %s %s +%d", ex, mkt, sym, tf, n)
                            except Exception as exc:
                                log.warning("%s %s %s %s failed: %s", ex, mkt, sym, tf, exc)
                            (empty if n is None else todo).append((mkt, db, source, sym, tf))
                fresh_s = time.monotonic() - began
                fast[:] = fast_new = fast_new + [(ex, *c) for c in todo]
                fast_at[0] = began_ms
                start = backlog.get(ex, 0) % max(1, len(todo))
                done = 0
                for i, (mkt, db, source, sym, tf) in enumerate(empty + todo[start:] + todo[:start]):
                    if i >= len(empty) and time.monotonic() - began - fresh_s > args.backfill_budget:
                        break
                    await quick()
                    try:
                        # at least the candles a strategy needs on this timeframe (250 daily
                        # candles are far more than --backfill-days)
                        n = await download(source, db, sym, tf, min(since, history_start(tf)))
                        if n:
                            log.info("%s %s %s %s +%d", ex, mkt, sym, tf, n)
                    except Exception as exc:
                        log.warning("%s %s %s %s failed: %s", ex, mkt, sym, tf, exc)
                    done += i >= len(empty)
                backlog[ex] = start + done
                log.info("%s pass: newest candles of %d charts in %.0fs, history of %d in %.0fs", ex,
                         len(todo) + len(empty),
                         fresh_s, done, time.monotonic() - began - fresh_s)
                if args.spot_history_days > 0:  # Spot AI's pairs keep their complete history, a bit each pass
                    try:
                        await deepen(adapter, store, jobs[0][2], s.timeframes,
                                     int(time.time() * 1000) - args.spot_history_days * 86_400_000,
                                     budget_s=args.history_budget)
                    except Exception as exc:
                        log.warning("%s spot history failed: %s", ex, exc)
            end = time.monotonic() + args.interval
            while (left := end - time.monotonic()) > 0:  # with Auto scan on, quick fetches meanwhile
                auto = wallet.autoscan(auto_path)
                await asyncio.sleep(min(left, auto) if 0 < auto < wallet.FULL_SCAN_SECONDS else left)
                await quick()
    finally:
        for a in adapters.values():
            await a.close()
        store.close()
        fstore.close()


async def cmd_stream(s: Settings, args) -> int:
    def on_tick(ex, sym, px, ts):
        print(f"{_fmt(ts)} {ex} {sym} {px}")

    if args.bybit:  # Bybit V5 public spot WebSocket: best bid / ask and last price
        from .data.bybit_ws import BybitSpotStream

        feed = BybitSpotStream(args.symbol or s.symbols, on_update=lambda q: print(
            f"{_fmt(q.ts)} bybit {q.symbol} bid {q.bid} ask {q.ask} last {q.last}", flush=True))
    else:
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


def cmd_indicators(s: Settings, args) -> int:
    """The 20 standard indicators on the last closed candle of a stored series (Phase 3)."""
    from .data.context import ContextStore, attach_context, has_context
    from .indicators import candles_to_frame
    from .indicators.results import closed_only, evaluate_all
    from .models import TIMEFRAME_MS

    store = CandleStore(s.db_path)
    try:
        candles = store.load(args.exchange, args.symbol, args.timeframe)
        if not candles:
            print("no candles stored for that series; run `aitrader download` first", file=sys.stderr)
            return 1
        df = closed_only(candles_to_frame(candles), int(time.time() * 1000), TIMEFRAME_MS[args.timeframe])
        if has_context(store):
            ctx = ContextStore(s.db_path)
            try:
                df = attach_context(df, ctx, args.exchange, args.symbol, args.timeframe)
            finally:
                ctx.close()
    finally:
        store.close()
    res = evaluate_all(df)
    if args.json:
        print(json.dumps({k: r.as_dict() for k, r in res.items()}, default=str, indent=1))
        return 0
    print(f"{args.symbol} {args.timeframe}, last closed candle {_fmt(int(df['ts'].iloc[-1]))} UTC")
    side = {1: "bullish", -1: "bearish", 0: "neutral"}
    for name, r in res.items():
        main = next(iter(r.features.items()), ("", float("nan")))
        note = r.metadata.get("zone") or r.metadata.get("regime") or r.metadata.get("bias") or ""
        print(f"  {name:15} {side[r.direction] if r.is_valid else 'not valid':10} conf {r.confidence:4.2f}  "
              f"{main[0]}={main[1]:.6g}  {note}{'' if r.is_valid else f' (needs {r.warmup_period} bars)'}")
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


def _context_symbols(s: Settings) -> list[str]:
    """The pairs to keep funding and open interest for: the fixed list plus what Spot AI and Futures AI
    trade now (their top gainers while those lists are on), read again on every round."""
    from .market.pairs import configured_symbols

    out = list(s.symbols)
    for market in ("spot", "futures"):
        try:
            out += configured_symbols(s.symbols, market=market)
        except Exception:  # a broken pair list must not stop the context feed
            logging.getLogger("aitrader.context").exception("can't read the %s pairs", market)
    from .data.perp import is_dated

    # funding and open interest belong to the perpetuals: a dated futures contract has neither
    return [p for p in dict.fromkeys(out) if not is_dated(p)]


async def cmd_context(s: Settings, args) -> int:
    """Market context (Phase 2): funding, open interest, global market cap; see docs/market-context.md."""
    import time

    from .data.context import HISTORY_DAYS, ContextStore, collect_context, mirror_context
    from .data.perp import futures_db_path
    from .strategies.context_filter import ContextFilter

    ctx = ContextStore(s.db_path)
    try:
        if args.context_cmd in ("collect", "backfill"):
            log = logging.getLogger("aitrader.context")
            history = args.context_cmd == "backfill"
            days = args.backfill_days if not history or args.backfill_days != 30 else HISTORY_DAYS
            while True:
                symbols = args.symbol or _context_symbols(s)
                if history:
                    print(f"fetching {days} days of funding, open interest, premium index and on-chain data for "
                          f"{len(symbols)} pairs; this takes a while", flush=True)
                added = await collect_context(ctx, symbols, backfill_days=days, history=history)
                log.info("context +%s", ", ".join(f"{m} {n}" for m, n in added.items() if n) or "nothing new")
                # Futures AI's database gets the perpetuals' series too (it never opens Spot AI's)
                fut = ContextStore(futures_db_path(s.db_path))
                try:
                    copied = mirror_context(ctx, fut)
                finally:
                    fut.close()
                if history:
                    print("rows saved: " + (", ".join(f"{m} {n}" for m, n in added.items() if n) or "none")
                          + f"; {copied} copied to Futures AI's database", flush=True)
                if args.once or history:
                    return 0
                await asyncio.sleep(args.interval)
        if args.context_cmd == "derivs":
            return await _print_derivs(args.symbol or s.symbols)
        now, symbol = int(time.time() * 1000), (args.symbol or ["BTC/USDT"])[0]
        for ex, sym in dict.fromkeys([(args.exchange, symbol), ("onchain", symbol), ("global", "*")]):
            for metric in ("funding_rate", "open_interest", "long_short_ratio", "premium_index", "mvrv", "nvt",
                           "active_addresses", "tx_count", "btc_dominance", "total_mcap",
                           "total_mcap_change_24h", "stable_mcap", "stable_supply"):
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


async def _print_derivs(symbols: list[str]) -> int:
    """Live perpetual readings from Bybit (public data, no key): last, mark and index price,
    basis, funding and open interest."""
    from .data.context import make_client
    from .data.derivs import fetch_snapshot

    client = make_client()
    try:
        print(f"{'pair':10} {'last':>12} {'mark':>12} {'index':>12} {'basis':>8} {'funding':>9} "
              f"{'per year':>9} {'next in':>8} {'open interest':>14}")
        for sym in symbols:
            try:
                d = await fetch_snapshot(client, sym)
            except Exception as exc:
                print(f"{sym:10} unavailable: {type(exc).__name__}: {exc}")
                continue

            def pct(v, digits=3):
                return "-" if v is None else f"{v * 100:+.{digits}f}%"

            nxt = d.funding_in_s()
            print(f"{sym:10} {d.last or 0:>12g} {d.mark or 0:>12g} {d.index or 0:>12g} {pct(d.basis):>8} "
                  f"{pct(d.funding_rate, 4):>9} {pct(d.funding_annualized, 1):>9} "
                  f"{'-' if nxt is None else f'{nxt / 3600:.1f}h':>8} {d.open_interest or 0:>14g}")
        return 0
    finally:
        await client.close()


def cmd_news(s: Settings, args) -> int:
    """News: free feeds plus the local model's brief (see aitrader.news)."""
    import time

    from .news import NewsFilter, load_latest, update
    from .ops import EventLog
    from .ops.secrets import apply_to_env

    def both_engines() -> list[str]:  # Spot AI's pairs and the Futures top gainers
        try:
            return list(dict.fromkeys([*configured_symbols(s.symbols, market="spot"),
                                       *configured_symbols(s.symbols, market="futures")]))
        except Exception:
            return list(s.symbols)

    symbols = args.symbol or both_engines()
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
                symbols = args.symbol or both_engines()  # the gainers change through the day
                out = update(symbols, events=events)
                last_brief = time.time()
                b = out["brief"]
                print(f"news: market risk {b['market_risk']} ({b['model']})" if b
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


def cmd_change(s: Settings, args) -> int:
    """Change requests from the dashboard: Claude writes the change and opens a pull request."""
    from .ops import changes

    if args.change_cmd == "list":
        for r in changes.list_requests():
            print(f"{r['id']} {r['status']:9} {r['pr_url'] or r['error'] or ''}\n    {r['text'][:120]}")
        return 0
    req = changes.run_saved(args.id)
    print(f"change {req['id']}: {req['status']} {req['pr_url'] or req['summary'] or req['error']}")
    return 0 if req["status"] in ("pr_open", "declined") else 1


def cmd_research(s: Settings, args) -> int:
    """Trader-research log (aitrader.news.research). Its search is turned off; `show` still reads it."""
    from .news import research

    if args.research_cmd == "show":
        rows = [f for f in research.load() if not args.family or f["family"] in args.family]
        for f in rows[:args.limit]:
            print(f"{f['date_found']} [{f['family']}] ({f['evidence']}) {f['claim']}\n    {f['url']}"
                  f"\n    {f['credibility']}")
        print(f"{len(rows)} findings (research only: they never change trading, risk or strategy status)")
        return 0
    print(research.OFF, file=sys.stderr)  # the saved findings stay readable with `research show`
    return 1


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
    from .learn import (Entry, Registry, RuleError, evaluate, extract_strategies, spec_id, PaperConfig,
                        PaperTrader)
    from .learn.registry import UNTESTED
    from .strategies import STRATEGIES

    reg = Registry(args.registry)
    exchanges = args.exchange or EXCHANGES
    symbols = args.symbol or s.symbols
    cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                         slippage_bps=s.paper_slippage_bps, max_cost_r=s.max_cost_r)

    def run_tests(entries, move: bool = True, market: str = "spot") -> None:
        from .learn.picks import learns_for

        # each engine tests on its own candles: Spot AI on spot, Futures AI on the perpetuals
        store, pairs, costs = _engine_market(s, market, symbols, cfg)
        regimes: dict = {}  # each market's regime labels, worked out once for all entries
        skipped = [e.id for e in entries if not learns_for(e, market)]
        if skipped:
            print(f"  {len(skipped)} left out: {'Spot AI never trades them' if market == 'spot' else 'Futures AI never trades them'}")
        for e in entries:
            if not learns_for(e, market):
                continue
            print(f"  testing {e.id} ...", flush=True)
            res = evaluate(e, store, exchanges, pairs, costs, move=move, regimes=regimes, market=market,
                           progress=lambda text: print(f"    {text}", flush=True))
            m = res["out_of_sample"]
            # Futures AI's test moves only the strategies Spot AI never trades (short twins,
            # Futures picks); a shared one keeps the status its spot test gave it
            if move and (market == "spot" or not learns_for(e, "spot")):
                verdict = {"paper": "PASSED -> paper trading", "rejected": "rejected",
                           "under_review": "UNDER REVIEW (holdout)"}.get(e.status, "untested")
            else:
                verdict = "no data" if not res["markets"] else "failed" if res["failed"] else "passed"
            wf, mc, h = res["walk_forward"], res["monte_carlo"], res["holdout"]
            print(f"  {e.id:40} {verdict:24} oos trades={m['trades']:4d} exp={m['expectancy_r']:+.3f}R"
                  f" sharpe={m['sharpe']:+.2f} sortino={m['sortino']:+.2f}"
                  f"\n      walk-forward {wf['positive']}/{wf['counted']} windows > 0R; Monte Carlo p95 DD "
                  f"{mc['max_drawdown_r_p95']:.1f}R, P(loss) {mc['loss_probability']:.0%}; holdout "
                  + (f"{h['trades']} trades {h['expectancy_r']:+.3f}R" if h["evaluated"] else "not looked at")
                  + (f"\n      why: {'; '.join(res['failed'])}" if res["failed"] else ""))
        store.close()
        reg.save()

    cmd = args.learn_cmd
    if cmd == "read":
        from . import llm
        from .learn.extract import LOCAL_REPAIR_CHARS, LOCAL_WORDS_PER_PART, local_caller

        print(f"reading {args.book} with the local model {llm.book_model_name()} (slow: many minutes "
              "per part on one CPU core) ...", flush=True)
        try:
            ex = extract_strategies(args.book, local_caller(), max_strategies=args.max,
                                    words_per_part=LOCAL_WORDS_PER_PART, repair_chars=LOCAL_REPAIR_CHARS,
                                    progress=lambda s: print(f"  {s}", flush=True))
        except Exception as exc:  # the local model unreachable, a broken file
            print(f"could not read {args.book}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        finally:
            llm.unload(llm.book_model_name())  # its ~2.5 GB back before the backtests start
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
        print(f"{len(new)} new strategies from \"{ex.book}\" -> {args.registry}", flush=True)
        if args.test and new:
            print("backtesting them:")
            run_tests(new)
        return 0
    if cmd == "read-queue":
        from .learn import bookqueue

        if args.run:
            bookqueue.serve(args.queue, args.registry)
            return 0
        row = bookqueue.run_next(args.queue, args.registry)
        print("nothing queued" if row is None else f"{row['book']}: {row['status']} {row.get('summary', '')}")
        return 0 if row is None or row["status"] == "done" else 1
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
            run_tests(reg.with_status("research"))
        return 0
    if cmd == "add-builtin":
        for name in args.names or list(STRATEGIES):
            e = Entry(id=f"builtin-{name}", kind="builtin", name=name, source=STRATEGIES[name].spec.source)
            print(("  + " if reg.add(e) else "  = ") + e.id)
        reg.save()
        return 0
    if cmd == "add-timeframe":
        from .learn.registry import make_strategy, timeframe_twin

        tf, near = args.timeframe, set(args.near.split(","))
        added = 0
        for e in list(reg.entries.values()):
            try:
                tfs = set(make_strategy(e).spec.timeframes)
            except Exception:
                continue
            if tf in tfs or not tfs & near:
                continue
            twin = timeframe_twin(e, tf)
            added += bool(twin) and reg.add(twin)
        reg.save()
        print(f"{added} new {tf} versions of strategies that trade {', '.join(sorted(near))} -> {args.registry}")
        return 0
    if cmd == "pick-timeframe":
        import time as _time

        from .learn import picks
        from .learn.report import known_entries

        mkt = args.market
        tf, path = args.timeframe, picks.picks_path(args.registry, mkt)
        if args.clear:
            gone = picks.clear(reg, tf, path, mkt)
            print(f"{tf} pick undone: every strategy may trade {tf} again; {len(gone)} pick copies stopped")
            return 0
        if args.choose is not None:  # the admin's own choice from the saved pick's results
            ids = [i for i in args.choose.split(",") if i]
            out = picks.choose(reg, known_entries(reg.entries), tf, path, ids, mkt, by=args.by or "")
            if out is None:
                print(f"no saved {tf} pick to choose from: run pick-timeframe {tf} first")
                return 1
            print(f"{tf}: kept the {len(out['picked'])} chosen strategies; {len(out['dropped'])} stopped trading {tf}")
            return 0
        now_ms = int(_time.time() * 1000)
        since = (_ms(args.since) if args.since else now_ms - int(args.years * 365.25 * 86_400_000))
        until = min(_ms(args.until), now_ms) if args.until else now_ms
        entries = known_entries(reg.entries)
        if args.only:  # only these strategies are judged (e.g. the ones the Replay page had loaded)
            only = set(args.only.split(","))
            entries = [e for e in entries if e.id in only]
        if mkt == "spot":  # Spot AI never sells short: its picks don't test the short twins
            entries = [e for e in entries if e.kind != "short"]
        from .data.perp import db_for

        store = CandleStore(db_for(s.db_path, mkt))  # Futures AI's pick is tested on perpetual candles
        try:
            if mkt == "futures":  # Futures AI's pick: longs costed as perpetuals too
                from .backtest.engine import as_perp

                cfg = as_perp(cfg)
            results = picks.evaluate(entries, store, exchanges[0], list(symbols), tf, since, cfg, until_ms=until)
            bars = {sym: store.count(exchanges[0], sym, tf, since, until + 1) for sym in symbols}
        finally:
            store.close()
        if not any(r["trades"] for r in results):
            print(f"no {tf} trades: download {tf} prices first (aitrader download --since ... --timeframe {tf})")
            return 0 if args.skip_empty else 1
        out = picks.apply(reg, entries, tf, results, path, min_trades=args.min_trades, market=mkt,
                          info={"since": since, "until": until, "pairs": list(symbols), "bars": bars,
                                **({} if args.since else {"years": args.years}),
                                **({"only": len(entries)} if args.only else {})})
        print(f"{tf}: tested {out['tested']} strategies on {len(symbols)} pairs; kept {len(out['picked'])} "
              f"(total above 0R, at least {args.min_trades} trades, up in at least half their quarters)")
        for r in picks.winners(results, args.min_trades):
            print(f"  {r['name']:<45} {r['trades']:>4} trades {r['win_rate']:>5.1f}% win {r['total_r']:+8.2f}R "
                  f"up {r['quarters_up']}/{r['quarters_up'] + r['quarters_down']} quarters")
        return 0
    if cmd == "add-shorts":
        from .learn.picks import picked_for
        from .learn.registry import short_twin

        added = 0  # a "WIN 4h" copy gets no twin: the short twin of its original has its own pick
        for e in [e for e in reg.entries.values() if e.kind != "short" and not picked_for(e)]:
            added += reg.add(short_twin(e))
        reg.save()
        print(f"{added} new short twins (Bybit USDT perpetual futures) -> {args.registry}")
        return 0
    if cmd == "paper-all":
        moved = 0
        for e in reg.with_status(*UNTESTED, "rejected", "under_review"):
            e.set_status("paper", "paper-test every strategy (turned on from the dashboard or `learn paper-all`)")
            moved += 1
        reg.save()
        n = len(reg.with_status("paper"))
        print(f"{moved} strategies moved to paper trading; {n} paper trading now "
              f"({len(reg.with_status('retired'))} retired after poor paper results stay retired)")
        return 0
    if cmd == "keep-top":
        from .learn.keep import indicators_used, keep_top, kept_ids, save_kept, save_n, saved_n

        # each engine ranks on its own: Spot AI the longs, Futures AI the longs and the short twins,
        # each by the backtest and its own paper trades; a strategy leaves paper trading only when
        # neither engine kept it
        markets = ["spot", "futures"] if args.market == "both" else [args.market]
        if len(markets) == 1 and kept_ids(args.registry, "futures" if markets[0] == "spot" else "spot") is None:
            # the other engine has never ranked: rank it too, or it would trade what this one kept
            markets.append("futures" if markets[0] == "spot" else "spot")
        name = {"spot": "Spot AI", "futures": "Futures AI"}
        for mkt in markets:
            other = "futures" if mkt == "spot" else "spot"
            n = save_n(args.registry, args.n, mkt) if args.n else saved_n(args.registry, mkt)
            out = keep_top(reg, n, market=mkt, others=kept_ids(args.registry, other) or set())
            reg.save()
            save_kept(args.registry, mkt, [k["id"] for k in out["kept"]])
            print(f"{name[mkt]}: the best {len(out['kept'])} strategies keep paper trading (at most {n}):")
            for i, k in enumerate(out["kept"], 1):
                tfs = f"  [{', '.join(k['timeframes'])}]" if k["timeframes"] else ""
                print(f"  {i:2d}. {k['id']:44} {k['score']:+.3f}R per trade{tfs}  {k['why']}"
                      f"{'' if k['passed'] else '  (did not pass the backtest)'}")
            untested = f", {out['untested']} had no backtest result yet" if out["untested"] else ""
            print(f"{len(out['benched'])} taken out of paper trading, {len(out['returned'])} brought back{untested}")
            used = indicators_used(reg, mkt, kept=kept_ids(args.registry, mkt))
            if used:
                print("indicators they use: " + ", ".join(f"{k} ({c})" for k, c in used.items()))
        return 0
    if cmd == "test":
        market = getattr(args, "market", "spot") or "spot"
        print(f"{'Futures AI: backtesting on the perpetuals' if market == 'futures' else 'Spot AI: backtesting on spot candles'}")
        if args.ids:
            entries = [reg.get(i) for i in args.ids]
        elif getattr(args, "missing", False):
            from .learn.keep import has_own_result
            from .learn.picks import learns_for

            # every strategy this engine trades that has no result on its own candles yet, whatever
            # its status (one Spot AI tested or rejected was never tried on the perpetuals)
            entries = [e for e in reg.entries.values() if not (e.spec or {}).get("pick")
                       and learns_for(e, market) and not has_own_result(e, market)]
            print(f"{len(entries)} strategies have no {'perpetual' if market == 'futures' else 'spot'} result yet")
        elif args.paper:
            entries = reg.with_status("paper")
        else:
            entries = reg.with_status(*UNTESTED, *(("rejected",) if args.retest else ()))
        if not entries:
            print("nothing to test (add strategies with `learn read` or `learn add-builtin`)")
            return 0
        # a missing-result run of Futures AI moves only its own strategies (``run_tests``)
        run_tests(entries, move=not args.paper, market=market)
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
        from .learn.ladder import Evidence, ladder_state, label

        ev = Evidence(registry_path=args.registry)
        for e in sorted(reg.entries.values(), key=lambda e: (e.status, e.id)):
            bt = e.backtest.get("out_of_sample", {})
            pp = e.paper or {}
            print(f"{label(ladder_state(e, ev)[0]):23} {e.id:40} bt oos {bt.get('trades', 0):4d} tr {bt.get('expectancy_r', 0):+.3f}R"
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
    if cmd == "improve":
        from .learn.improve import improve

        entries = [reg.get(i) for i in args.ids] if args.ids else reg.with_status("paper")
        if not entries:
            print("nothing to improve: no strategy is paper trading", file=sys.stderr)
            return 1
        store = CandleStore(s.db_path)
        try:
            rep = improve(store, reg, entries, exchanges, symbols, cfg, args.max_evals, args.report)
        finally:
            store.close()
        reg.save()
        for r in rep["results"]:
            ch = ", ".join(f"{k} {a:g}->{b:g}" for k, (a, b) in r["changes"].items())
            print(f"  {r['strategy']:40} tried {r['tried']:3d}  {r['outcome']}" + (f"\n      {ch}" if ch else "")
                  + (f"\n      new: {r['new_id']}" if r.get("new_id") else ""))
        print(f"{rep['improved']} of {len(rep['results'])} strategies improved")
        return 0
    if cmd in ("teach", "teach-on", "teach-off"):
        return _teach(s, args, reg, exchanges, symbols, cfg)
    if cmd == "paper":
        from .events import CalendarFilter
        from .news import NewsFilter

        from .ai import LiveFilter
        from .learn.teacher import TeacherFilter
        from .learn.wallet import MARKETS, engine_path

        from .ops import EventLog
        from .trade import Control

        store = CandleStore(s.db_path)
        context = None
        if not args.no_context:
            from .data.context import ContextStore
            from .strategies.context_filter import ContextFilter

            context = ContextFilter(ContextStore(store))
        pairs = symbols if args.symbol else (lambda: configured_symbols(s.symbols))  # re-read each tick
        # Spot AI trades the Spot scanner's gainers, Futures AI the Futures scanner's
        per_market = None if args.symbol else (lambda m: configured_symbols(s.symbols, market=m))
        from .data.perp import futures_db_path

        trader = PaperTrader(store, args.registry, exchanges, pairs,
                             PaperConfig(start_equity=s.paper_start_usdt, backtest=cfg),
                             calendar=None if args.no_calendar else CalendarFilter(),
                             # each engine its own AI filter and teacher (Futures AI: *_futures folders)
                             ai_filter=None if args.no_ai else {m: LiveFilter(engine_path(args.models, m))
                                                                for m in MARKETS},
                             control=Control(os.path.join(TRADE_DIR, "control.json")),
                             context=context, events=EventLog(service="paper"),
                             news=None if args.no_news else NewsFilter(),
                             teacher=None if args.no_teacher else {m: TeacherFilter(engine_path(args.teacher_dir, m))
                                                                   for m in MARKETS},
                             market_symbols=per_market,
                             futures_store=CandleStore(futures_db_path(s.db_path)))
        try:
            if args.once:
                print(trader.tick())
            else:
                trader.run(args.interval)
        finally:
            store.close()
        return 0
    raise RuleError(f"unknown learn command {cmd}")


def _teach(s: Settings, args, reg, exchanges, symbols, cfg) -> int:
    """``learn teach`` / ``teach-on`` / ``teach-off``: the backtest teacher (learn/teacher.py)."""
    from .learn import make_strategy
    from .learn.teacher import Teacher, collect, describe, teach
    from .ops import EventLog

    from .learn.picks import learns_for
    from .learn.wallet import engine_path

    market = getattr(args, "market", "spot") or "spot"
    name = "Futures AI" if market == "futures" else "Spot AI"
    teacher = Teacher(engine_path(args.dir, market), events=EventLog(service="cli"))
    if args.learn_cmd == "teach-on":
        try:
            t = teacher.turn_on(args.by)
        except ValueError as exc:
            print(f"not turned on: {exc}", file=sys.stderr)
            return 1
        print(f"{name} paper trading now follows lessons {t['version']}; `learn teach-off` stops it")
        return 0
    if args.learn_cmd == "teach-off":
        teacher.turn_off(args.by or "")
        print(f"{name} paper trading no longer follows the teacher's lessons")
        return 0
    entries = (list(reg.entries.values()) if args.all else reg.with_status("paper", "retired"))
    strategies = []
    for e in [e for e in entries if learns_for(e, market)]:  # what this engine could trade
        try:
            strategies.append((e.id, make_strategy(e)))
        except Exception as exc:  # one broken entry must not stop the lesson
            print(f"  skipping {e.id}: {exc}", file=sys.stderr)
    if not strategies:
        print("no strategies to learn from: start paper trading first, or pass --all", file=sys.stderr)
        return 1
    # Futures AI learns on the perpetuals' own candles, costed as perps; Spot AI on spot candles
    store, symbols, cfg = _engine_market(s, market, symbols, cfg)
    print(f"{name}: backtesting {len(strategies)} strategies on {len(symbols)} pairs "
          f"({'perpetual' if market == 'futures' else 'spot'} candles); one line per strategy", flush=True)
    try:
        frame = collect(store, strategies, exchanges, symbols, cfg,
                        progress=lambda text: print(f"  {text}", flush=True))
    finally:
        store.close()
    if frame.empty:
        print("no backtest signals: download history first (`aitrader download`)", file=sys.stderr)
        return 1
    result = teach(frame)
    teacher.save_candidate(result)
    w, f = result["check"]["without"], result["check"]["with"]
    c = result["counts"]
    print(f"{name} lessons {result['version']}: {result['signals']} backtest signals from {len(strategies)} strategies "
          f"(learned on {result['learn_signals']}, checked on {result['check_signals']} they never saw)")
    print(f"  {c['trade']} situations to trade, {c['avoid']} to avoid, {c['unproven']} unproven; "
          f"1R profit-take helps in {result['exit_1r']}")
    print(f"  unseen part without lessons: {w['trades']} trades, win rate {w['win_rate'] or 0:.0%}, "
          f"{w['expectancy_r'] or 0:+.3f}R per trade")
    print(f"  unseen part with lessons:    {f['trades']} trades, win rate {f['win_rate'] or 0:.0%}, "
          f"{f['expectancy_r'] or 0:+.3f}R per trade")
    if result["passed"]:
        print("PASSED: turn it on from the dashboard (Teacher page) or `aitrader learn teach-on --by <name>"
              + (" --market futures`" if market == "futures" else "`"))
    else:
        print(f"not usable: {result['why']}")
    best = sorted(((sid, key, x) for sid, by in result["lessons"].items() for key, x in by.items()
                   if x["verdict"] == "trade"), key=lambda t: -t[2]["own"]["shrunk_r"])[:10]
    for sid, key, x in best:
        print(f"  {sid:36} {describe(key):42} {x['own']['trades']:4d} trades "
              f"{x['own']['expectancy_r']:+.3f}R" + ("  (take profit at 1R)" if x["exit_1r"] else ""))
    return 0


def _filter_strategies(reg, use_all: bool, market: str = "spot") -> list:
    """(id, strategy) pairs ``market``'s filter learns from: by default the strategies that passed
    the backtest gate (paper, or retired after paper); with ``use_all`` every builtin and rule.
    Only what that engine could trade: Spot AI never shorts, and a pick copy is its engine's."""
    from .learn import make_strategy
    from .learn.picks import learns_for
    from .learn.registry import Entry
    from .strategies import STRATEGIES

    if not use_all:  # Futures AI's shorts too: learned (and judged) as longs on the flipped chart
        entries = list(reg.with_status("paper", "retired"))
    else:
        entries = [e for e in reg.entries.values() if e.kind in ("rule", "short")]
        entries += [Entry(id=f"builtin-{n}", kind="builtin", name=n) for n in STRATEGIES]
    return [(e.id, make_strategy(e)) for e in entries if learns_for(e, market)]


def _engine_market(s: Settings, market: str, symbols: list[str], cfg):
    """The candles one engine learns on: (store, pairs, backtest costs). Futures AI: the perpetuals'
    own database, every pair stored there, perp fees and funding. Spot AI: spot candles as given."""
    if market != "futures":
        return CandleStore(s.db_path), symbols, cfg
    from .backtest.engine import as_perp
    from .data.perp import futures_db_path
    from .ops.doctor import _newest_candles

    store = CandleStore(futures_db_path(s.db_path))
    return store, sorted(_newest_candles(store.conn)) or symbols, as_perp(cfg) if cfg is not None else None


def cmd_ai(s: Settings, args) -> int:
    """Phase 4 trade filter: build the training set, train and test, promote, roll back."""
    import getpass

    from .ai import ModelRegistry, TrainConfig, build_dataset, load_dataset, save_dataset, train
    from .backtest import BacktestConfig
    from .learn import Registry

    from .learn.wallet import engine_path

    market = getattr(args, "market", "spot") or "spot"  # each engine its own filter and training set
    models = ModelRegistry(engine_path(args.models, market))
    args.dataset = engine_path(args.dataset, market)
    cmd = args.ai_cmd

    def make_dataset():
        reg = Registry(args.registry)
        strategies = _filter_strategies(reg, args.all, market)
        if not strategies:
            print("no strategies to learn from: run `aitrader learn add-library --test` (or "
                  "`learn add-builtin` + `learn test`) first, or pass --all", file=sys.stderr)
            return None
        cfg = BacktestConfig(taker_fee=s.paper_taker_fee, maker_fee=s.paper_maker_fee,
                             slippage_bps=s.paper_slippage_bps, max_cost_r=s.max_cost_r)
        from .data.perp import futures_db_path

        store, fstore = CandleStore(s.db_path), CandleStore(futures_db_path(s.db_path))
        # Futures AI's filter learns on the perpetuals' candles and Futures AI's own paper trades
        market_store, pairs, _ = _engine_market(s, market, args.symbol or s.symbols, None)
        market_store.close()
        try:
            frame = build_dataset(fstore if market == "futures" else store, strategies,
                                  args.exchange or EXCHANGES, args.symbol or pairs, cfg, args.include_final,
                                  None if args.no_paper else args.paper_state,
                                  futures_store=fstore if market != "futures" else None, market=market)
        finally:
            store.close()
            fstore.close()
        save_dataset(frame, args.dataset)
        print(f"{market}: {len(frame)} signals from {len(strategies)} strategies -> {args.dataset}")
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
        store = _engine_market(s, market, [], None)[0]
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
    from .data.perp import futures_db_path

    for path in (s.db_path, futures_db_path(s.db_path)):  # spot, then Futures AI's perpetual candles
        store = CandleStore(path)
        try:
            applied = store.migrate()
            print(f"database {path}: schema v{store.schema_version()}"
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
        store.backup(os.path.join(args.dest, os.path.basename(s.db_path)), candles=not getattr(args, "no_candles", False))
    finally:
        store.close()
    from .data.perp import futures_db_path

    fdb = futures_db_path(s.db_path)
    if os.path.exists(fdb) and not getattr(args, "no_candles", False):  # Futures AI's perpetual candles
        fstore = CandleStore(fdb)
        try:
            fstore.backup(os.path.join(args.dest, os.path.basename(fdb)))
        finally:
            fstore.close()
    for path in ("data/learn/registry.json", "data/learn/paper_state.json", "data/ai/models/registry.json",
                 "data/trade/live_state.json", "data/trade/control.json",
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
    # served over HTTPS (AITRADER_SITE set): the session cookie never goes over plain http
    https = bool((e("AITRADER_SITE") or "").strip()) and not (e("AITRADER_SITE") or "").strip().startswith("http://")
    secure = e("AITRADER_WEB_SECURE_COOKIE", "true" if https else "false").strip().lower() in ("1", "true", "yes")
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
    """Pause, resume, kill switch, status, and the live-pilot executor."""
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
    from .trade.golive import approval_intact

    mode = args.mode
    events = _events(f"executor-{mode}")
    strategies = set(args.strategy or []) or None
    if mode == "live":
        assert_live_allowed(s)  # the three live-lock lines in .env, opened by you
        pilot = float(os.environ.get("AITRADER_PILOT_MAX_USDT", "0") or 0)
        if pilot <= 0:
            print("set AITRADER_PILOT_MAX_USDT in .env: the most USDT the live pilot may use", file=sys.stderr)
            return 1
        gl = GoLive(events=events, cfg=GateConfig.from_env())
        approval = gl._load("approval.json")
        approved = sorted((approval.get("strategies") or {}))
        if approved and not approval_intact(approval):
            print("the go-live approval was edited or not made by `golive approve`: approve again", file=sys.stderr)
            return 1
        if not approved:
            print("no go-live approval: run `aitrader golive check` then `aitrader golive approve`",
                  file=sys.stderr)
            return 1
        strategies = (strategies or set(approved)) & set(approved)
        if not strategies or len(strategies) > 2:
            print("the live pilot runs 1 or 2 approved strategies", file=sys.stderr)
            return 1
        guard, monitor = gl.guard, gl.monitor
    adapter = make_adapter(args.exchange, s)
    ex = Executor(adapter, mode, args.exchange, PAPER_STATE,
                  state_path=os.path.join(TRADE_DIR, f"{mode}_state.json"), strategies=strategies,
                  control=control, risk=RiskEngine(RiskLimits.from_env()), events=events,
                  live_guard=guard, monitor=monitor, pilot_max_usdt=pilot)
    stream = None
    if not args.once and args.exchange == "bybit":
        from .data.bybit_ws import BybitSpotStream

        # streamed prices: stops checked every second, quiet coins get no new entries
        from .ops import feedstats

        ex.prices = BybitSpotStream(s.symbols)
        stream = asyncio.create_task(ex.prices.run())
        reporter = asyncio.create_task(feedstats.report(ex.prices))  # the Monitor page's feed health
    try:
        if args.once:
            print(json.dumps(await ex.tick(), indent=1))
        else:
            await ex.run(args.interval)
    finally:
        if stream is not None:
            ex.prices.stop()
            stream.cancel()
            reporter.cancel()
        await adapter.close()
    return 0


def confirm_live_approval(strategy_ids: list[str], ask=input, isatty=None) -> str | None:
    """LIVE ENABLED needs a person at a terminal typing the phrase that names the strategies.
    There is deliberately no --yes flag, and piped input is refused. Returns what was typed, or
    None when it wasn't confirmed."""
    from .trade.golive import confirmation_phrase

    if not (isatty or sys.stdin.isatty)():
        print("approving live trading needs a person: run it in a terminal "
              "(with docker: `docker compose run --rm -it collector golive approve ...`)", file=sys.stderr)
        return None
    phrase = confirmation_phrase(strategy_ids)
    print("This lets the live executor place REAL orders with real money for: " + ", ".join(sorted(strategy_ids)))
    print("Nothing here promises a profit. To approve, type exactly:\n  " + phrase)
    try:
        typed = ask("> ").strip()
    except EOFError:
        typed = ""
    if typed != phrase:
        print("not approved", file=sys.stderr)
        return None
    return typed


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
        phrase = confirm_live_approval(args.strategy or [])
        if phrase is None:
            return 1
        try:
            gl.approve(args.strategy or [], by=args.by, confirmation=phrase)
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
                keys = ", ".join(k["exchange"] for k in store.key_info(u.id, testnet=False))
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



def cmd_pairs(s: Settings, args) -> int:
    """Rank Bybit spot USDT pairs from public data, or show the ranking and current regimes."""
    from .market import pairs
    from .market.regime import current_regimes

    if args.pairs_cmd == "rank":
        async def run():
            client = pairs.public_client(args.exchange)
            try:
                return await pairs.rank_pairs(client, pairs.RankConfig(min_turnover_usdt=args.min_turnover,
                                                                       depth_top=args.depth_top),
                                              args.registry, PAPER_STATE, exchange=args.exchange)
            finally:
                await client.close()

        rk = asyncio.run(run())
        pairs.save_ranking(rk, args.out)
        print(pairs.ranking_text(rk, args.top))
        print(f"ranking saved to {args.out}")
        return 0
    if args.pairs_cmd == "gainers":
        from .market import gainers, perp_limits

        gm = args.market
        gpath = gainers.path_for(gm)
        name = "Spot" if gm == "spot" else "Futures"
        if args.off:
            gainers.switch(False, path=gpath)
            print(f"{name} top gainers off: trading uses the usual pairs (open positions still close normally)")
            return 0

        async def fetch():
            client = gainers.client_for(gm, args.exchange)
            try:
                return await gainers.refresh(client, gpath, top=args.count, market=gm, window=args.window,
                                             limits_path=perp_limits.DEFAULT_PATH)
            finally:
                await client.close()

        gainers.switch(True, args.count, path=gpath, window=args.window,
                       contract=args.contract if gm == "futures" else None)
        data = asyncio.run(fetch())
        hot = gainers.symbols(gpath)
        dated = gm == "futures" and gainers.contract_of(data) == "dated"
        floor = gainers.DATED_MIN_TURNOVER if dated else gainers.MIN_TURNOVER
        what = (gainers.CONTRACTS[gainers.contract_of(data)] if gm == "futures" else f"{name} top gainers")
        print(f"{what}, top {data['top']} over {data['window']} (24h turnover over "
              f"{floor / 1e6:g}M USDT, spread at most {gainers.MAX_SPREAD_PCT:g}%):")
        for p in data["pairs"][:data["top"]]:
            print(f"  {p['symbol']:<22} score {p['score']:+7.2f}  24h {p['change']:+7.2f}%  "
                  f"turnover {p['turnover'] / 1e6:,.0f}M" + (f"  NOT TRADED: {p['pump']}" if p.get("pump") else ""))
        print(f"trading them in paper now ({len(hot)}); the collector downloads their prices on its next pass")
        return 0
    if args.pairs_cmd == "squeeze":
        from .market import gainers, squeeze

        spath = squeeze.path_from_env()
        if args.off:
            squeeze.switch(False, path=spath)
            print("squeeze scanner off: Futures AI is back to its top gainers (open positions still close normally)")
            return 0

        async def scan():
            client = gainers.perp_client(args.exchange)
            try:
                return await squeeze.refresh(client, spath, top=args.count)
            finally:
                await client.close()

        squeeze.switch(True, args.count, path=spath)
        data = asyncio.run(scan())
        print(f"squeeze pairs ({data['scanned']} perpetuals scanned; Bollinger width in the lowest "
              f"{squeeze.WIDTH_PCT:.0%} of the week and last hour's volume z-score over {squeeze.MIN_Z:g}):")
        for p in data["pairs"][:data["top"]]:
            print(f"  {p['symbol']:<14} volume z {p['vol_z']:+6.2f}  1h {p['change_1h']:+6.2f}%  "
                  f"turnover {p['turnover'] / 1e6:,.0f}M")
        if not data["pairs"]:
            print("  none right now (the collector scans again every hour while it is on)")
        return 0
    if args.pairs_cmd == "show":
        rk = pairs.load_ranking(args.out)
        if not rk.get("pairs"):
            print("no pair ranking yet: run `aitrader pairs rank`")
            return 1
        print(pairs.ranking_text(rk, args.top))
        return 0
    store = CandleStore(s.db_path)
    try:
        rows = current_regimes(store, args.symbol or configured_symbols(s.symbols),
                               args.timeframe or ["1h", "4h", "1d"], args.exchange_data or EXCHANGES)
    finally:
        store.close()
    for r in rows:
        print(f"{r['symbol']:12} {r['timeframe']:4} {r['label'] or '-':16} {r['confidence']:.2f}  {r['reason']}")
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
    d.add_argument("--market", choices=("spot", "futures"), default="spot",
                   help="spot candles (Spot AI) or USDT perpetual candles in the futures database (Futures AI)")

    c = sub.add_parser("check-data", help="run data-quality checks on stored candles")
    c.add_argument("--strict", action="store_true", help="exit 1 if any series has problems")

    co = sub.add_parser("collect", help="service: keep candles up to date in the database")
    co.add_argument("--exchange", action="append", choices=EXCHANGES)
    co.add_argument("--interval", type=float, default=60, help="seconds between top-ups")
    co.add_argument("--backfill-days", type=int, default=30, help="history to fetch on first run")
    co.add_argument("--spot-history-days", type=int, default=1095,
                    help="Spot AI's pairs: keep this many days of history on every timeframe, fetched a bit "
                         "each pass (default 1095, 3 years; 0 = off)")
    co.add_argument("--backfill-budget", type=float, default=120,
                    help="seconds per pass for history and hole filling after the newest candles")
    co.add_argument("--history-budget", type=float, default=45,
                    help="seconds per pass spent fetching that older history")

    st = sub.add_parser("stream", help="print live prices from the websocket feed")
    st.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    st.add_argument("--symbol", action="append")
    st.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    st.add_argument("--bybit", action="store_true", help="Bybit V5 public spot WebSocket (bid/ask/last)")

    ps = sub.add_parser("paper-smoke", help="open and close a test position on the paper exchange")
    ps.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    ps.add_argument("--symbol", default="BTC/USDT")
    ps.add_argument("--usdt", type=float, default=100.0)

    ind = sub.add_parser("indicators", help="the 20 standard indicators (direction, confidence, validity) "
                         "on the last closed candle of a stored series")
    ind.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES)
    ind.add_argument("--symbol", default="BTC/USDT")
    ind.add_argument("--timeframe", default="1h")
    ind.add_argument("--json", action="store_true")

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

    nw = sub.add_parser("news", help="world and crypto news: feeds plus the local AI model's brief")
    nw.add_argument("news_cmd", choices=["update", "run", "show"])
    nw.add_argument("--symbol", action="append")
    nw.add_argument("--every-hours", type=float, default=None,
                    help="for run: hours between briefs (default AITRADER_NEWS_EVERY_HOURS or 4)")

    ch = sub.add_parser("change", help="change requests (turned off: they needed the Anthropic API)")
    ch_sub = ch.add_subparsers(dest="change_cmd", required=True)
    ch_sub.add_parser("list", help="every change request with its status")
    chr_ = ch_sub.add_parser("run", help="run one saved request (the dashboard does this)")
    chr_.add_argument("id")

    rs = sub.add_parser("research", help="trader-research log: public reports on our strategy families "
                                         "(research only, never changes trading)")
    rs.add_argument("research_cmd", choices=["update", "run", "show"], help="update and run are turned off")
    rs.add_argument("--family", action="append", help="limit to a strategy family (repeatable)")
    rs.add_argument("--limit", type=int, default=50, help="for show: findings to print")

    ca = sub.add_parser("calendar", help="show upcoming high-impact events and the current decision")
    ca.add_argument("--hours", type=float, default=72)

    cx = sub.add_parser("context", help="market context: funding, open interest, total market cap; "
                        "derivs: live mark/index price, basis and funding of the perpetuals")
    cx.add_argument("context_cmd", choices=["collect", "backfill", "show", "derivs"],
                    help="backfill: years of funding, open interest and premium index for the ML strategies")
    cx.add_argument("--symbol", action="append")
    cx.add_argument("--exchange", default=EXCHANGE, choices=EXCHANGES, help="for show")
    cx.add_argument("--once", action="store_true", help="collect: one top-up, then exit")
    cx.add_argument("--interval", type=float, default=900, help="collect: seconds between top-ups")
    cx.add_argument("--backfill-days", type=int, default=30,
                    help="collect: history on first run; backfill: days back (default 1100)")

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
    lr = le_sub.add_parser("read", help="have the local book model read a book and draft strategies (slow)")
    lr.add_argument("book", help="PDF, Markdown or text file")
    lr.add_argument("--max", type=int, default=12, help="most strategies to keep from one book")
    lr.add_argument("--test", action="store_true", help="backtest the new drafts straight away")
    lq = le_sub.add_parser("read-queue", help="read the next book uploaded on the dashboard; with --run, "
                                              "the books service: read queued books at night")
    lq.add_argument("--run", action="store_true", help="keep running, reading during AITRADER_BOOK_HOURS")
    lq.add_argument("--queue", default="data/books/queue.json")
    ll = le_sub.add_parser("add-library", help="register the strategies already read from the supplied books")
    ll.add_argument("--book", help="only books whose title contains this")
    ll.add_argument("--test", action="store_true", help="backtest them straight away")
    lb = le_sub.add_parser("add-builtin", help="register the hand-coded strategies too")
    lb.add_argument("names", nargs="*")
    lat = le_sub.add_parser("add-timeframe", help="add a copy of every long strategy that trades a nearby "
                                                  "timeframe, running on this one (default 30m, from 15m and 1h)")
    lat.add_argument("timeframe", nargs="?", default="30m", choices=["5m", "15m", "30m", "1h", "4h", "1d"])
    lat.add_argument("--near", default="15m,1h", help="copy strategies that trade any of these (comma separated)")
    lpt = le_sub.add_parser("pick-timeframe", help="on one timeframe, trade only the strategies that made "
                                                   "money there steadily (the rest stop trading it)")
    lpt.add_argument("timeframe", nargs="?", default="4h", choices=["5m", "15m", "30m", "1h", "4h", "1d"])
    lpt.add_argument("--years", type=float, default=2.0, help="how much saved history to test (default 2), "
                     "when --since isn't given")
    lpt.add_argument("--since", help="test from this UTC date (YYYY-MM-DD), e.g. the period loaded on Replay")
    lpt.add_argument("--until", help="test up to this UTC date (default: now)")
    lpt.add_argument("--only", help="judge only these strategy ids (comma separated); the rest stop trading it")
    lpt.add_argument("--min-trades", type=int, default=10, help="fewest trades to judge a strategy (default 10)")
    lpt.add_argument("--clear", action="store_true", help="undo the pick: every strategy trades it again")
    lpt.add_argument("--choose", help="keep exactly these tested strategy ids (comma separated) from the saved "
                                      "pick, whatever the rules say; no new backtest")
    lpt.add_argument("--by", help="who chose (shown with the pick)")
    lpt.add_argument("--skip-empty", action="store_true",
                     help="no trades on this timeframe: leave it as it is and exit 0 (for a run over every timeframe)")
    lpt.add_argument("--market", choices=["spot", "futures"], default="spot",
                     help="whose pick: Spot AI's (default) or Futures AI's; each engine trades only its own")
    le_sub.add_parser("add-shorts", help="add a short twin (Bybit perpetual futures) of every long strategy")
    le_sub.add_parser("paper-all", help="paper-trade every strategy, including untested and rejected "
                                        "ones (fake money; poor ones retire on their paper results)")
    lk = le_sub.add_parser("keep-top", help="keep only the best N strategies paper trading (ranked by "
                                            "backtest and paper results, with a few places for the best "
                                            "on each timeframe); the rest leave paper trading")
    lk.add_argument("--n", type=int, help="how many to keep, 10-20 (default: the last number used, else 15)")
    lk.add_argument("--market", choices=["spot", "futures", "both"], default="both",
                    help="whose ranking: Spot AI's, Futures AI's, or each engine's own (default)")
    lt = le_sub.add_parser("test", help="backtest candidates; passes go to paper trading")
    lt.add_argument("ids", nargs="*")
    lt.add_argument("--retest", action="store_true", help="also re-test rejected strategies")
    lt.add_argument("--paper", action="store_true", help="backtest the strategies already paper trading and "
                                                        "record the result, without moving them")
    lt.add_argument("--missing", action="store_true",
                    help="test every strategy that has no result on this engine's own candles yet")
    lt.add_argument("--market", choices=["spot", "futures"], default="spot",
                    help="whose test: Spot AI's on spot candles (default) or Futures AI's on the perpetuals")
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
    lim = le_sub.add_parser("improve", help="try many settings for each paper strategy on older data, keep a change "
                                            "only if it also wins on newer data and passes the full gate")
    lim.add_argument("ids", nargs="*", help="strategies to improve (default: every one paper trading)")
    lim.add_argument("--max-evals", type=int, default=30, help="most backtests per strategy in the search")
    lim.add_argument("--report", default="data/learn/improve.json")
    lte = le_sub.add_parser("teach", help="backtest teacher: learn which market situations (trend, volatility, "
                                          "volume) each strategy wins and loses in, check it on unseen data")
    lte.add_argument("--all", action="store_true", help="every strategy in the registry, not only the ones "
                                                       "paper trading or retired after paper")
    lte.add_argument("--dir", default="data/learn/teacher")
    lte.add_argument("--market", choices=("spot", "futures"), default="spot",
                     help="whose lessons: Spot AI's (spot candles) or Futures AI's (perpetuals, in <dir>_futures)")
    lton = le_sub.add_parser("teach-on", help="paper trading follows the newest lessons (they must pass the check)")
    lton.add_argument("--by", required=True)
    lton.add_argument("--dir", default="data/learn/teacher")
    lton.add_argument("--market", choices=("spot", "futures"), default="spot")
    ltoff = le_sub.add_parser("teach-off", help="paper trading stops following the lessons")
    ltoff.add_argument("--by")
    ltoff.add_argument("--dir", default="data/learn/teacher")
    ltoff.add_argument("--market", choices=("spot", "futures"), default="spot")
    lp = le_sub.add_parser("paper", help="service: paper-trade every strategy that passed")
    lp.add_argument("--interval", type=float, default=60)
    lp.add_argument("--once", action="store_true")
    lp.add_argument("--no-calendar", action="store_true", help="ignore the economic calendar filter")
    lp.add_argument("--no-news", action="store_true", help="ignore the AI news filter")
    lp.add_argument("--no-ai", action="store_true", help="ignore the active AI trade filter")
    lp.add_argument("--models", default="data/ai/models", help="trade-filter model registry folder")
    lp.add_argument("--no-context", action="store_true", help="ignore the market-context filter")
    lp.add_argument("--no-teacher", action="store_true", help="ignore the backtest teacher's lessons")
    lp.add_argument("--teacher-dir", default="data/learn/teacher")

    sub.add_parser("migrate", help="bring the database schema up to date")
    bk = sub.add_parser("backup", help="copy the database and registries into a folder")
    bk.add_argument("dest")
    bk.add_argument("--no-candles", action="store_true",
                    help="leave the price candles out (they can be downloaded again): a few MB instead of GBs")

    w = sub.add_parser("web", help="dashboard: submit books, see strategies and results (login required)")
    w.add_argument("--host", default="127.0.0.1", help="0.0.0.0 only inside Docker or behind a proxy")
    w.add_argument("--port", type=int, default=8080)

    ai = sub.add_parser("ai", help="Phase 4 AI trade filter: training set, train/test, promote, rollback")
    ai.add_argument("--models", default="data/ai/models", help="model registry folder")
    ai.add_argument("--dataset", default="data/ai/dataset.csv")
    ai.add_argument("--registry", default="data/learn/registry.json", help="strategy registry")
    ai.add_argument("--market", choices=("spot", "futures"), default="spot",
                    help="whose filter: Spot AI's or Futures AI's (models, training set: <name>_futures)")
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
    trr.add_argument("--mode", choices=["live"], default="live")
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

    pa = sub.add_parser("pairs", help="rank Bybit spot USDT pairs; show the ranking and market regimes")
    pa.add_argument("pairs_cmd", choices=["rank", "show", "regimes", "gainers", "squeeze"])
    pa.add_argument("--off", action="store_true", help="gainers/squeeze: stop trading that list")
    pa.add_argument("--out", default="data/pairs/ranking.json", help="ranking file")
    pa.add_argument("--top", type=int, default=30, help="rows to print")
    pa.add_argument("--count", type=int, help="gainers: how many to trade (3-30, default 10); squeeze: 1-15, default 5")
    pa.add_argument("--market", choices=["spot", "futures"], default="futures",
                    help="gainers: the Spot scanner (Spot AI) or the Futures scanner (Futures AI)")
    pa.add_argument("--window", choices=["1h", "4h", "24h"], help="gainers: rank by the rise over this window")
    pa.add_argument("--contract", choices=["perpetual", "dated"],
                    help="gainers --market futures: the USDT perpetuals or the dated (expiring) USDT futures")
    pa.add_argument("--exchange", default="bybit", help="rank: ccxt exchange id (public data only)")
    pa.add_argument("--min-turnover", type=float, default=2_000_000, help="rank: 24h USDT turnover floor")
    pa.add_argument("--depth-top", type=int, default=40, help="rank: order books to check")
    pa.add_argument("--registry", default="data/learn/registry.json")
    pa.add_argument("--symbol", action="append", help="regimes: pairs (default: the configured list)")
    pa.add_argument("--timeframe", action="append", help="regimes: default 1h, 4h, 1d")
    pa.add_argument("--exchange-data", action="append", help="regimes: stored candles to use")

    us = sub.add_parser("users", help="user accounts: list, approve, disable, live on/off, run")
    us_sub = us.add_subparsers(dest="users_cmd", required=True)
    us_sub.add_parser("list", help="every account with its status, mode and connected keys")
    for name, h in (("approve", "let a new account connect its key (nothing trades until live-on)"),
                    ("disable", "block an account: sells what the agent bought for it and stops it"),
                    ("live-on", "allow an account to switch itself to real money"),
                    ("live-off", "stop real money for an account (sells what the agent bought)"),
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
    from .ops.redact import install

    install()  # no key, signature or signed URL is ever written to a log
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
        "indicators": cmd_indicators,
        "strategies": cmd_strategies,
        "backtest": cmd_backtest,
        "calendar": cmd_calendar,
        "context": cmd_context,
        "news": cmd_news,
        "research": cmd_research,
        "change": cmd_change,
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
        "pairs": cmd_pairs,
    }[args.cmd]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
