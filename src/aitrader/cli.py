"""Command line entry point: ``aitrader <command>``. Run ``aitrader --help`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import logging
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
    }[args.cmd]
    result = handler(settings, args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
