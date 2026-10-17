"""The Replay page: watch a backtest play out candle by candle from a chosen month.

Pick a pair, a timeframe, a start month and a strategy (all of them, the best 10, 20 or 30, or
one). The server runs the strategies over that window (``learn.replay``) and works out the
chart indicators for every candle; the page's script (``static/replay.js``) then plays the
candles one at a time at 1x to 200x speed, draws the indicators those strategies use, marks each
buy and short as its candle appears, reads the market at the current candle in plain words and
keeps a live win-rate table per strategy. Nothing on the chart looks past the current candle.

Prices come from the database. A month that hasn't been downloaded yet can be fetched from
Bybit with one button (a ``download`` job). While it runs, the page shows a bar with how many of
the period's candles are saved so far (``/api/replay/download/<job>``) and loads the replay by
itself when the download ends.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .. import clock
from ..config import EXCHANGE
from ..backtest import BacktestConfig
from ..backtest.engine import as_perp
from ..data.perp import db_for
from ..data.store import CandleStore
from ..learn import keep, picks
from ..learn.registry import Registry
from ..learn.replay import MAX_BARS, WARMUP_BARS, replay
from ..learn.report import known_entries
from ..models import TIMEFRAME_MS
from . import studies
from .app import _csrf, _e, _q
from .market_pages import STATIC, _study_chips

REPLAY_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
PICK_ALL_ORDER = ["1d", "4h", "1h", "30m", "15m", "5m"]  # "every timeframe": the fewest candles first
SPEEDS = [1, 2, 5, 10, 50, 200]
CACHE_SIZE = 6
TOPS = [10, 20, 30]
MAX_MONTHS = 36  # Replay's longest period: 3 years
LENGTHS = [("1", "1 month"), ("2", "2 months"), ("3", "3 months"), ("6", "6 months"), ("12", "1 year"),
           ("24", "2 years"), ("36", "3 years")]
PICK_YEARS = 2  # history a timeframe pick tests (learn.picks)


def _top(strategy: str) -> int:
    """10, 20 or 30 for a "top:N" choice, else 0."""
    try:
        n = int(strategy.split(":", 1)[1]) if strategy.startswith("top:") else 0
    except ValueError:
        return 0
    return n if n in TOPS else 0


def timeframe_ranking(rows: list[dict], tf: str) -> list[str]:
    """Strategy ids best first by their backtest-report result on timeframe ``tf`` alone: the
    average R per trade over every pair, shrunk toward 0 for few trades (as ``keep.score``).
    Strategies with no trades on ``tf`` in the report are left out."""
    scored = []
    for r in rows:
        on_tf = [m for m in r.get("markets", []) if m.get("timeframe") == tf]
        n = sum(m.get("trades", 0) for m in on_tf)
        if n:
            scored.append((sum(m.get("total_r", 0.0) for m in on_tf) / (n + keep.PRIOR), r["id"]))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [i for _s, i in scored]


def replay_studies(store: CandleStore, exchange: str, res: dict) -> dict:
    """The chart indicators for each shown candle, warmed up on the candles before it. Only the
    per-candle series: support/resistance, Fibonacci and the volume profile are worked out by the
    page from the candles up to the current one, so the chart never shows a level from the future."""
    if not res["candles"]:
        return {}
    step = TIMEFRAME_MS[res["timeframe"]]
    rows = store.load(exchange, res["symbol"], res["timeframe"], start=res["start"] - WARMUP_BARS * step,
                      end=res["end"])
    candles = [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in rows]
    out = studies.compute(candles, tail=len(res["candles"]))
    return {"overlays": out["overlays"], "panels": out["panels"], "patterns": out["patterns"]}


def _month(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _day_ms(day: str) -> int:
    """Midnight UTC of a YYYY-MM-DD date, as the ``download`` command reads it."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def download_status(status: str, have: int, since_ms: int, until_ms: int, tf: str, error: str = "",
                    ahead: str = "") -> dict:
    """How far a download job has got: the share of the period's candles already saved. Bybit can
    have gaps (or a pair listed later), so only a finished job counts as 100%."""
    want = max(1, (until_ms - since_ms) // TIMEFRAME_MS[tf])
    done = status == "done"
    pct = 100 if done else min(99, int(have * 100 / want))
    return {"status": status, "have": have, "want": want, "pct": pct, "error": error, "ahead": ahead}


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1)


def add_replay_routes(app, *, settings, auth, jobs, page, guard, registry_path: str,
                      report_path: str = "data/reports/backtest.json", exchange: str = EXCHANGE,
                      paper_state: str = "") -> None:
    cache: OrderedDict = OrderedDict()

    def futures_mode() -> bool:
        from ..learn import wallet

        return bool(paper_state) and wallet.futures_on(wallet.market_path(paper_state))

    def ws() -> str:
        return "futures" if futures_mode() else "spot"

    def pairs() -> list[str]:
        """The workspace's pairs: Futures AI's (its top gainers while that list is on) in the Futures
        view, Spot AI's in the Spot view."""
        from ..market.pairs import configured_symbols

        try:
            return configured_symbols(settings.symbols, market="futures" if futures_mode() else "spot")
        except Exception:
            return list(settings.symbols)

    def cfg() -> BacktestConfig:
        base = BacktestConfig(taker_fee=settings.paper_taker_fee, maker_fee=settings.paper_maker_fee,
                              slippage_bps=settings.paper_slippage_bps, max_cost_r=settings.max_cost_r)
        base = as_perp(base) if futures_mode() else base  # Futures mode: longs costed as perpetuals too
        if paper_state:  # the engine's minimum reward and split take-profit, as paper trading uses them
            from dataclasses import replace

            from ..learn import guards

            g = guards.load(guards.path_for(paper_state))["futures" if futures_mode() else "spot"]
            base = replace(base, min_rr=g["min_rr"], split_tp=g["split_tp"], maker_entry=g["maker_entry"],
                           breakeven=g["split_tp"] and g["breakeven"])
        return base

    def report_rows() -> list[dict]:
        try:
            with open(report_path, encoding="utf-8") as fh:
                return json.load(fh).get("strategies", [])
        except (OSError, ValueError):
            return []

    def ranked_strategies() -> list[tuple[str, str]]:
        """(id, label) for the strategy menu, best 5-year win rate first when a report exists."""
        rows = report_rows()
        out = [(r["id"], f'{r["name"]} · {r["all"]["win_rate"]:.0f}% win, {r["all"]["total_r"]:+.0f}R')
               for r in sorted(rows, key=lambda r: (r["all"]["win_rate"] if r["all"]["trades"] >= 30 else -1,
                                                    r["all"]["total_r"]), reverse=True)]
        if not out:
            out = [(e.id, e.name) for e in known_entries(Registry(registry_path).entries)]
        return out

    def best_ids(entries, tf: str) -> tuple[list[str], int]:
        """Every strategy id, best first, and how many of them were ranked on ``tf`` itself: the
        backtest report's results on ``tf``, then the paper-trading ranking (``keep.rank``), then
        the report's overall win rate, then the rest."""
        on_tf = timeframe_ranking(report_rows(), tf)
        try:
            ranked = [e.id for e, _s in keep.rank(Registry(registry_path))]
        except Exception:
            ranked = []
        ids = on_tf + ranked + [i for i, _label in ranked_strategies()] + [e.id for e in entries]
        return list(dict.fromkeys(ids)), len(on_tf)

    def picks_file() -> str:
        """The workspace engine's own picks (Spot AI's or Futures AI's)."""
        return picks.picks_path(registry_path, "futures" if futures_mode() else "spot")

    def compute(symbol: str, tf: str, start: datetime, months: int, strategy: str) -> dict:
        tf_picks = picks.load(picks_file())
        key = (symbol, tf, start, months, strategy, (tf_picks.get(tf) or {}).get("at"), futures_mode())
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        mkt = "futures" if futures_mode() else "spot"
        # the workspace's strategies: no WIN copies made by the other engine's picks
        entries = [e for e in known_entries(Registry(registry_path).entries) if picks.trades_now(e, False, mkt)]
        if not futures_mode():  # Spot AI never shorts: its replay leaves the short twins out
            entries = [e for e in entries if e.kind != "short"]
        store = CandleStore(db_for(settings.db_path, mkt))  # Futures view: the perpetuals' candles
        try:
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(_add_months(start, months).timestamp() * 1000)
            n = _top(strategy)
            on_tf = 0
            if strategy == "win":  # only this engine's WIN strategies for this timeframe
                only = [e.id for e in entries if picks.picked_for(e) == tf]
            elif n:
                only, on_tf = best_ids(entries, tf)
            else:
                only = "" if strategy.startswith("top:") else strategy
            # a pick that kept nothing: replay every strategy anyway so they can be looked at, and say
            # that none of them trades this timeframe on this engine
            none_kept = tf in tf_picks and not (tf_picks[tf] or {}).get("picked")
            shown = {t: v for t, v in tf_picks.items() if t != tf} if none_kept else tf_picks
            res = replay(entries, store, exchange, symbol, tf, start_ms, end_ms, only=only, cfg=cfg(), top=n,
                         tf_picks=shown)
            res["picked"] = tf in tf_picks and not none_kept
            res["none_kept"] = none_kept
            res["top"] = n
            res["win_only"] = strategy == "win"
            ranked = set(only[:on_tf]) if n else set()
            res["ranked_on_tf"] = sum(1 for s in res["strategies"] if s["id"] in ranked)
            res["studies"] = replay_studies(store, exchange, res)
            by_id = {e.id: e for e in entries}
            used: dict[str, int] = {}
            for s in res["strategies"]:
                try:
                    keys = keep.indicators(by_id[s["id"]])
                except Exception:
                    keys = set()
                s["indicators"] = sorted(keys)
                for k in keys:
                    used[k] = used.get(k, 0) + 1
            res["indicators"] = dict(sorted(used.items(), key=lambda kv: (-kv[1], kv[0])))
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
        if symbol not in pairs() or tf not in REPLAY_TIMEFRAMES or start is None:
            return JSONResponse({"error": "pick a pair, a timeframe and a month"}, status_code=400)
        if start > datetime.now(timezone.utc):
            return JSONResponse({"error": "that month hasn't happened yet"}, status_code=400)
        months = min(max(int(months or 1), 1), MAX_MONTHS)
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
        if symbol not in pairs() or tf not in REPLAY_TIMEFRAMES or start is None:
            return RedirectResponse("/replay?error=" + _q("pick a pair, a timeframe and a month"), status_code=303)
        months = min(max(int(months or 1), 1), MAX_MONTHS)
        since = (start - timedelta(milliseconds=WARMUP_BARS * TIMEFRAME_MS[tf])).strftime("%Y-%m-%d")
        # no more than Replay can show (MAX_BARS candles): 3 years of 5m candles would be wasted
        until = min(_add_months(start, months), datetime.now(timezone.utc),
                    start + timedelta(milliseconds=MAX_BARS * TIMEFRAME_MS[tf])).strftime("%Y-%m-%d")
        job_id = jobs.submit(f"Download {symbol} {tf} for {month}",
                             [["download", "--since", since, "--until", until, "--market", ws(),
                               "--exchange", exchange, "--symbol", symbol, "--timeframe", tf]],
                             meta={"kind": "download", "by": user, "symbol": symbol, "tf": tf, "market": ws(),
                                   "since_ms": _day_ms(since), "until_ms": _day_ms(until)})
        cache.clear()
        q = f"symbol={_q(symbol)}&tf={_q(tf)}&month={_q(month)}&months={months}"
        return RedirectResponse(f"/replay?{q}&download={job_id}", status_code=303)

    @app.get("/api/replay/download/{job_id}")
    def download_progress(request: Request, job_id: str):
        _user, _token, redirect = guard(request)
        if redirect:
            return JSONResponse({"error": "sign in"}, status_code=401)
        try:
            job = jobs.get(job_id)
        except (OSError, ValueError, KeyError):
            return JSONResponse({"error": "no such download"}, status_code=404)
        meta = job.get("meta", {})
        if meta.get("kind") != "download" or "since_ms" not in meta:
            return JSONResponse({"error": "no such download"}, status_code=404)
        ahead = ""
        if job["status"] == "queued":  # only another download can be ahead of it
            running = [j for j in jobs.list() if j["status"] == "running"
                       and j.get("meta", {}).get("kind") == "download"]
            ahead = running[0]["title"] if running else ""
        store = CandleStore(db_for(settings.db_path, meta.get("market", "spot")))
        try:
            have = store.count(exchange, meta["symbol"], meta["tf"], meta["since_ms"], meta["until_ms"])
        finally:
            store.close()
        return JSONResponse(download_status(job["status"], have, meta["since_ms"], meta["until_ms"],
                                            meta["tf"], job.get("error", ""), ahead))

    @app.post("/replay/pick")
    def pick(request: Request, csrf: str = Form(""), tf: str = Form("4h"), undo: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if tf not in REPLAY_TIMEFRAMES and not (tf == "all" and not undo):
            return RedirectResponse("/replay?error=" + _q("pick a timeframe"), status_code=303)
        if any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "pick"
               for j in jobs.list()):
            return RedirectResponse(f"/replay?tf={_q(tf)}&error=" + _q("A pick is already running; see Recent "
                                                                       "jobs on the Overview page."), status_code=303)
        if undo:
            where = "Futures AI" if futures_mode() else "Spot AI"
            jobs.submit(f"Undo the {where} {tf} pick",
                        [["learn", "pick-timeframe", tf, "--clear", "--market",
                          "futures" if futures_mode() else "spot"]],
                        meta={"kind": "pick", "by": user})
            msg = f"Undoing the {where} {tf} pick: every strategy may trade {tf} on {where} again."
        elif tf == "all":
            since = (datetime.now(timezone.utc) - timedelta(days=round(PICK_YEARS * 365.25))).strftime("%Y-%m-%d")
            sym_args = [a for sym in pairs() for a in ("--symbol", sym)]
            where, mkt = ("Futures AI", "futures") if futures_mode() else ("Spot AI", "spot")
            if not sym_args:
                return RedirectResponse("/replay?error=" + _q(f"{where} has no pairs to test yet"), status_code=303)
            steps = []
            for t in PICK_ALL_ORDER:  # the quick timeframes first, 5m (the most candles) last
                steps += [["download", "--since", since, "--market", mkt, "--exchange", exchange, "--timeframe", t,
                           *sym_args],
                          ["learn", *sym_args, "pick-timeframe", t, "--years", str(PICK_YEARS),
                           "--market", mkt, "--skip-empty"]]
            jobs.submit(f"Keep only the winners on every timeframe on {where} pairs", steps,
                        meta={"kind": "pick", "by": user, "market": mkt})
            msg = (f"Testing every strategy on each timeframe ({', '.join(PICK_ALL_ORDER)}) on {PICK_YEARS:g} years "
                   f"of prices for the {len(pairs())} {where} pairs. On each timeframe the steady winners become "
                   f"{where}'s WIN strategies and the rest stop trading it there; the other engine is not changed. "
                   "It takes a while: each timeframe's result appears here as soon as it is done.")
            tf = "1h"
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=round(PICK_YEARS * 365.25))).strftime("%Y-%m-%d")
            # the workspace's own pairs: in the Futures view only Futures AI's (its top gainers)
            sym_args = [a for sym in pairs() for a in ("--symbol", sym)]
            where = "Futures AI" if futures_mode() else "Spot AI"
            jobs.submit(f"Keep only the {tf} winners on {where} pairs",
                        [["download", "--since", since, "--market", ws(), "--exchange", exchange, "--timeframe", tf,
                          *sym_args],
                         ["learn", *sym_args, "pick-timeframe", tf, "--years", str(PICK_YEARS),
                          "--market", "futures" if futures_mode() else "spot"]],
                        meta={"kind": "pick", "by": user, "market": "futures" if futures_mode() else "spot"})
            msg = (f"Testing every {tf} strategy on {PICK_YEARS:g} years of {tf} prices for the {len(pairs())} "
                   f"{where} pairs. "
                   f"The steady winners keep trading {tf} on {where} only, as its new WIN strategies; the rest "
                   f"stop trading {tf} there, and the other engine is not changed. This page shows the "
                   "list when it's done (reload it in a few minutes).")
        cache.clear()
        return RedirectResponse(f"/replay?tf={_q(tf)}&msg=" + _q(msg), status_code=303)

    def pick_card(tf: str, token: str) -> str:
        p = picks.load(picks_file())
        cur = p.get(tf)
        others = ", ".join(t for t in REPLAY_TIMEFRAMES if t in p and t != tf)
        opts = "".join(f'<option value="{t}"{" selected" if t == tf else ""}>{t}</option>' for t in REPLAY_TIMEFRAMES)
        head = (f'{_e(tf)} pick on: {len(cur.get("picked", []))} strategies kept' if cur
                else "Keep only the winners on one timeframe")
        out = (f'<details class="card" id="tf-pick" data-tf="{_e(tf)}"><summary><b>{head}</b></summary>'
               '<p class="small muted">Tests every strategy that trades the chosen timeframe on '
               f'{PICK_YEARS:g} years of its prices for this workspace\'s pairs only (in the Futures view the Futures AI '
               'pairs, in the Spot view the Spot AI pairs), after fees. A strategy is kept when its '
               f'total is above 0R over at least {picks.MIN_TRADES} trades and it made money in at least as '
               'many 3-month quarters as it lost in, so one lucky quarter is not enough. Kept strategies '
               'become this engine\'s WIN strategies on that timeframe; the others stop trading it on this engine '
               '(their other timeframes carry on). Each engine has its own picks.</p>'
               f'<form method="post" action="/replay/pick" class="row">{_csrf(auth, token)}'
               f'<label>Timeframe <select name="tf">{opts}</select></label>'
               '<button>Keep only the winners</button></form>'
               f'<form method="post" action="/replay/pick" class="row">{_csrf(auth, token)}'
               '<input type="hidden" name="tf" value="all">'
               '<button class="ghost">Keep the winners on every timeframe</button></form>')
        if others:
            out += f'<p class="small">Also picked: {_e(others)}.</p>'
        if cur:
            won = [r for r in cur.get("results", []) if r["id"] in set(cur.get("picked", []))]
            when = clock.local(cur["at"] / 1000)
            out += (f'<p><b>{_e(tf)} pick ({_e(when)}):</b> {len(won)} of {len(cur.get("results", []))} '
                    f'strategies kept on {len(cur.get("pairs", []))} pairs: {_e(", ".join(cur.get("pairs", [])))}.</p>')
            if not won and cur.get("results"):
                out += (f'<p class="warn">No strategy passed on {_e(tf)}, so none trades {_e(tf)} on this engine now '
                        'and Replay shows none. That is the test working: after fees none made money steadily on '
                        f'these pairs. Undo the {_e(tf)} pick to let them all trade it again, or use a timeframe '
                        'whose pick kept some.</p>')
            if won:
                rows = "".join(
                    f'<tr><td>{_e(r["name"])}</td><td>{r["trades"]}</td><td>{r["win_rate"]:.0f}%</td>'
                    f'<td class="good">{r["total_r"]:+.2f}R</td><td>{r["quarters_up"]} of '
                    f'{r["quarters_up"] + r["quarters_down"]}</td></tr>' for r in won)
                out += ('<table><tr><th>Strategy</th><th>Trades</th><th>Win %</th>'
                        f'<th>Total</th><th>Quarters up</th></tr>{rows}</table>')
            out += _tested_table(cur, tf)
            out += (f'<form method="post" action="/replay/pick" class="row">{_csrf(auth, token)}'
                    f'<input type="hidden" name="tf" value="{_e(tf)}"><input type="hidden" name="undo" value="1">'
                    f'<button class="ghost">Undo the {_e(tf)} pick</button></form>')
        return out + '</details>'

    @app.get("/replay")
    def replay_page(request: Request, symbol: str = "", tf: str = "1h", month: str = "", months: int = 1,
                    strategy: str = "", msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        symbols = pairs()
        symbol = symbol if symbol in symbols else (symbols[0] if symbols else "")
        tf = tf if tf in REPLAY_TIMEFRAMES else "1h"
        now = datetime.now(timezone.utc)
        month = month if _month(month) else f"{now.year - 1}-{now.month:02d}"
        months = min(max(int(months or 1), 1), MAX_MONTHS)

        def options(items, current):
            return "".join(f'<option value="{_e(v)}"{" selected" if v == current else ""}>{_e(label)}</option>'
                           for v, label in items)

        tops = [(f"top:{n}", f"Top {n} strategies (best ranked)") for n in TOPS]
        strat_opts = options([("", "All strategies (books and built-in)"),
                              ("win", "WIN strategies only (kept by Keep only the winners)"), *tops,
                              *ranked_strategies()], strategy)
        speeds = "".join(f'<button type="button" class="seg{" active" if s == 1 else ""}" data-speed="{s}">{s}x</button>'
                         for s in SPEEDS)
        body = (f'<p class="good">{_e(msg)}</p>' if msg else "") + (f'<p class="bad">{_e(error)}</p>' if error else "")
        body += ('<p class="small muted">Futures AI workspace: only Futures AI\'s pairs, with long and short '
                 'strategies costed as perpetual futures.</p>' if futures_mode() else
                 '<p class="small muted">Spot AI workspace: only Spot AI\'s pairs and long strategies (Spot never '
                 'shorts).</p>')
        body += (
            '<div id="replay">'
            '<form id="replay-form" class="card replay-form">'
            f'<label>Pair <select name="symbol">{options([(s, s) for s in symbols], symbol)}</select></label>'
            f'<label>Timeframe <select name="tf">{options([(t, t) for t in REPLAY_TIMEFRAMES], tf)}</select></label>'
            f'<label>From month <input type="month" name="month" value="{_e(month)}" '
            f'max="{now.year}-{now.month:02d}"></label>'
            f'<label>Length <select name="months">{options(LENGTHS, str(months))}'
            '</select></label>'
            f'<label class="wide">Strategy <select name="strategy">{strat_opts}</select></label>'
            '<button type="submit">Load</button></form>'
            '<div id="replay-msg" class="muted"></div>'
            + pick_card(tf, token) +
            f'<form id="replay-download" method="post" action="/replay/download" class="row hidden">{_csrf(auth, token)}'
            '<input type="hidden" name="symbol"><input type="hidden" name="tf"><input type="hidden" name="month">'
            '<input type="hidden" name="months"><button>Download these prices from Bybit</button></form>'
            '<div id="replay-progress" class="card hidden" role="status" aria-live="polite">'
            '<p id="progress-text" class="small"></p>'
            '<div class="bar progress"><span id="progress-fill" style="width:0%"></span></div></div>'
            '<div class="card chart-card"><div class="chart-head"><h2 id="chart-title">Replay</h2>'
            f'<div class="segs" id="speeds">{speeds}</div></div>'
            '<div class="row replay-controls"><button type="button" id="play" disabled>Play</button>'
            '<button type="button" class="ghost" id="step" disabled>Next candle</button>'
            '<button type="button" class="ghost" id="restart" disabled>Restart</button>'
            '<input type="range" id="seek" min="0" max="0" value="0" aria-label="position" disabled>'
            '<span id="clock" class="small muted"></span></div>'
            f'<div id="studies" class="studies" aria-label="indicators">{_study_chips(skip={"trades"})}</div>'
            '<p id="studies-used" class="small muted"></p>'
            '<div id="ohlc" class="ohlc small"></div>'
            '<div id="legend" class="legend"></div>'
            '<canvas id="chart" aria-label="replay chart"></canvas>'
            '<p id="read" class="small market-read"></p></div>'
            '<div class="stats" id="replay-stats"></div>'
            '<h2>Strategies so far</h2><p class="muted small">Closed trades up to the current candle, best first. '
            'Win % is the share of trades that made money after fees and slippage; R is the amount risked per '
            'trade.</p><div id="board"></div>'
            '<h2>Trades</h2><div id="log"></div></div>'
            '<noscript><p class="bad">The replay needs JavaScript.</p></noscript>'
            '<p class="muted small">After Load, the indicators the chosen strategies use switch on (the number on '
            'a switch is how many of them use it); tap a switch to show or hide one. Indicators use the strategies\' '
            'own formulas and only the candles up to the current one: support/resistance from the last 300, '
            'Fibonacci and the volume profile from the last 100. Triangles mark entries (up: buy, down: short), '
            'dots the exits (green: profit, red: loss).</p>'
            '<script src="/static/chartstudies.js" defer></script><script src="/static/replay.js" defer></script>')
        return page("Replay backtest", body, user, token)


def _tested_table(cur: dict, tf: str) -> str:
    """Every strategy the pick tested, best first, with why each one was or wasn't kept, and how much
    price history each pair had."""
    results = cur.get("results") or []
    if not results:
        return ""
    kept = set(cur.get("picked", []))
    rows = "".join(
        f'<tr><td>{_e(r["name"])}</td><td>{r["trades"]}</td><td>{r["win_rate"]:.0f}%</td>'
        f'<td class="{"good" if r["total_r"] > 0 else "bad"}">{r["total_r"]:+.2f}R</td>'
        f'<td>{r["quarters_up"]} of {r["quarters_up"] + r["quarters_down"]}</td>'
        f'<td class="small">{"<span class=good>kept</span>" if r["id"] in kept else _e(picks.why_not(r, cur.get("min_trades", picks.MIN_TRADES)))}</td></tr>'
        for r in sorted(results, key=lambda r: r["total_r"], reverse=True))
    out = (f'<details><summary class="small">All {len(results)} tested strategies and why each was or wasn\'t kept'
           '</summary><table><tr><th>Strategy</th><th>Trades</th><th>Win %</th><th>Total</th>'
           f'<th>Quarters up</th><th>Result</th></tr>{rows}</table>')
    bars = cur.get("bars") or {}
    step = TIMEFRAME_MS.get(tf)
    if bars and step and cur.get("since") and cur.get("at"):
        full = max(1, (cur["at"] - cur["since"]) // step)
        out += ('<p class="small muted">Price history tested: ' + ", ".join(
            f'{_e(sym)} {n:,} candles ({min(100, 100 * n // full)}%)' for sym, n in bars.items())
            + '. A coin listed recently has less.</p>')
    return out + '</details>'
