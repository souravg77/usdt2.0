"""The Replay page: watch a backtest play out candle by candle from a chosen month.

Pick a pair, a timeframe, a start month and a strategy (or all of them). The server runs the
strategies over that window (``learn.replay``); the page's script (``static/replay.js``) then
plays the candles one at a time at 1x to 200x speed, marks each buy and sell as its candle
appears, and keeps a live win-rate table per strategy.

Prices come from the database. A month that hasn't been downloaded yet can be fetched from
Binance with one button (a ``download`` job).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from ..backtest import BacktestConfig
from ..data.store import CandleStore
from ..learn.registry import Registry
from ..learn.replay import WARMUP_BARS, replay
from ..learn.report import known_entries
from ..models import TIMEFRAME_MS
from .app import _csrf, _e, _q
from .market_pages import STATIC

REPLAY_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
SPEEDS = [1, 2, 5, 10, 50, 200]
CACHE_SIZE = 6


def _month(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1)


def add_replay_routes(app, *, settings, auth, jobs, page, guard, registry_path: str,
                      report_path: str = "data/reports/backtest.json", exchange: str = "binance") -> None:
    symbols = list(settings.symbols)
    cache: OrderedDict = OrderedDict()

    def cfg() -> BacktestConfig:
        return BacktestConfig(taker_fee=settings.paper_taker_fee, maker_fee=settings.paper_maker_fee,
                              slippage_bps=settings.paper_slippage_bps)

    def ranked_strategies() -> list[tuple[str, str]]:
        """(id, label) for the strategy menu, best 5-year win rate first when a report exists."""
        try:
            with open(report_path, encoding="utf-8") as fh:
                rows = json.load(fh).get("strategies", [])
        except (OSError, ValueError):
            rows = []
        out = [(r["id"], f'{r["name"]} · {r["all"]["win_rate"]:.0f}% win, {r["all"]["total_r"]:+.0f}R')
               for r in sorted(rows, key=lambda r: (r["all"]["win_rate"] if r["all"]["trades"] >= 30 else -1,
                                                    r["all"]["total_r"]), reverse=True)]
        if not out:
            out = [(e.id, e.name) for e in known_entries(Registry(registry_path).entries)]
        return out

    def compute(symbol: str, tf: str, start: datetime, months: int, strategy: str) -> dict:
        key = (symbol, tf, start, months, strategy)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        entries = known_entries(Registry(registry_path).entries)
        store = CandleStore(settings.db_path)
        try:
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(_add_months(start, months).timestamp() * 1000)
            res = replay(entries, store, exchange, symbol, tf, start_ms, end_ms, only=strategy, cfg=cfg())
        finally:
            store.close()
        cache[key] = res
        while len(cache) > CACHE_SIZE:
            cache.popitem(last=False)
        return res

    @app.get("/api/replay")
    async def api_replay(request: Request, symbol: str = "", tf: str = "1h", month: str = "",
                         months: int = 1, strategy: str = ""):
        _user, _token, redirect = guard(request)
        if redirect:
            return JSONResponse({"error": "sign in"}, status_code=401)
        start = _month(month)
        if symbol not in symbols or tf not in REPLAY_TIMEFRAMES or start is None:
            return JSONResponse({"error": "pick a pair, a timeframe and a month"}, status_code=400)
        if start > datetime.now(timezone.utc):
            return JSONResponse({"error": "that month hasn't happened yet"}, status_code=400)
        months = min(max(int(months or 1), 1), 3)
        res = await asyncio.to_thread(compute, symbol, tf, start, months, strategy)
        return JSONResponse(res)

    @app.get("/static/replay.js")
    def replay_js():
        with open(os.path.join(STATIC, "replay.js"), encoding="utf-8") as fh:
            return Response(fh.read(), media_type="text/javascript")

    @app.post("/replay/download")
    def download(request: Request, csrf: str = Form(""), symbol: str = Form(""), tf: str = Form("1h"),
                 month: str = Form(""), months: int = Form(1)):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        start = _month(month)
        if symbol not in symbols or tf not in REPLAY_TIMEFRAMES or start is None:
            return RedirectResponse("/replay?error=" + _q("pick a pair, a timeframe and a month"), status_code=303)
        months = min(max(int(months or 1), 1), 3)
        since = start - timedelta(milliseconds=WARMUP_BARS * TIMEFRAME_MS[tf])
        until = min(_add_months(start, months), datetime.now(timezone.utc))
        jobs.submit(f"Download {symbol} {tf} for {month}",
                    [["download", "--since", since.strftime("%Y-%m-%d"), "--until", until.strftime("%Y-%m-%d"),
                      "--exchange", exchange, "--symbol", symbol, "--timeframe", tf]],
                    meta={"kind": "download", "by": user})
        cache.clear()
        q = f"symbol={_q(symbol)}&tf={_q(tf)}&month={_q(month)}&months={months}"
        return RedirectResponse(f"/replay?{q}&msg=" + _q("Downloading those prices from Binance. Press Load "
                                                         "again in a minute."), status_code=303)

    @app.get("/replay")
    def replay_page(request: Request, symbol: str = "", tf: str = "1h", month: str = "", months: int = 1,
                    strategy: str = "", msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        symbol = symbol if symbol in symbols else (symbols[0] if symbols else "")
        tf = tf if tf in REPLAY_TIMEFRAMES else "1h"
        now = datetime.now(timezone.utc)
        month = month if _month(month) else f"{now.year - 1}-{now.month:02d}"
        months = min(max(int(months or 1), 1), 3)

        def options(items, current):
            return "".join(f'<option value="{_e(v)}"{" selected" if v == current else ""}>{_e(label)}</option>'
                           for v, label in items)

        strat_opts = options([("", "All strategies (books and built-in)"), *ranked_strategies()], strategy)
        speeds = "".join(f'<button type="button" class="seg{" active" if s == 1 else ""}" data-speed="{s}">{s}x</button>'
                         for s in SPEEDS)
        body = (f'<p class="good">{_e(msg)}</p>' if msg else "") + (f'<p class="bad">{_e(error)}</p>' if error else "")
        body += (
            '<div id="replay">'
            '<form id="replay-form" class="card replay-form">'
            f'<label>Pair <select name="symbol">{options([(s, s) for s in symbols], symbol)}</select></label>'
            f'<label>Timeframe <select name="tf">{options([(t, t) for t in REPLAY_TIMEFRAMES], tf)}</select></label>'
            f'<label>From month <input type="month" name="month" value="{_e(month)}" '
            f'max="{now.year}-{now.month:02d}"></label>'
            f'<label>Length <select name="months">{options([("1", "1 month"), ("2", "2 months"), ("3", "3 months")], str(months))}'
            '</select></label>'
            f'<label class="wide">Strategy <select name="strategy">{strat_opts}</select></label>'
            '<button type="submit">Load</button></form>'
            '<div id="replay-msg" class="muted"></div>'
            f'<form id="replay-download" method="post" action="/replay/download" class="row hidden">{_csrf(auth, token)}'
            '<input type="hidden" name="symbol"><input type="hidden" name="tf"><input type="hidden" name="month">'
            '<input type="hidden" name="months"><button>Download these prices from Binance</button></form>'
            '<div class="card chart-card"><div class="chart-head"><h2 id="chart-title">Replay</h2>'
            f'<div class="segs" id="speeds">{speeds}</div></div>'
            '<div class="row replay-controls"><button type="button" id="play" disabled>Play</button>'
            '<button type="button" class="ghost" id="step" disabled>Next candle</button>'
            '<button type="button" class="ghost" id="restart" disabled>Restart</button>'
            '<input type="range" id="seek" min="0" max="0" value="0" aria-label="position" disabled>'
            '<span id="clock" class="small muted"></span></div>'
            '<div id="ohlc" class="ohlc small"></div>'
            '<canvas id="chart" aria-label="replay chart"></canvas></div>'
            '<div class="stats" id="replay-stats"></div>'
            '<h2>Strategies so far</h2><p class="muted small">Closed trades up to the current candle, best first. '
            'Win % is the share of trades that made money after fees and slippage; R is the amount risked per '
            'trade.</p><div id="board"></div>'
            '<h2>Trades</h2><div id="log"></div></div>'
            '<noscript><p class="bad">The replay needs JavaScript.</p></noscript>'
            '<script src="/static/replay.js" defer></script>')
        return page("Replay backtest", body, user, token)
