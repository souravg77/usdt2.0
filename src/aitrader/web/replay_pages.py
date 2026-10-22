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
# Shawn: a pick tests at least this many months on slow charts (1 month of 4h is ~180 candles, of
# 1d only 30, too few for any strategy to show 10 trades); a shorter loaded period starts earlier
MIN_PICK_MONTHS = {"4h": 6, "1d": 12}
MIN_TRADE_CHOICES = (1, 3, 5, 10, 20, 30)  # "Fewest trades" a pick asks of a strategy
MAX_MONTHS = 36  # Replay's longest period: 3 years
LENGTHS = [("1", "1 month"), ("2", "2 months"), ("3", "3 months"), ("6", "6 months"), ("12", "1 year"),
           ("24", "2 years"), ("36", "3 years")]


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
        """The workspace's pairs, the same list as its Markets page: Futures AI's (its top gainers while
        that list is on) in the Futures view, Spot AI's in the Spot view, plus the coins that engine
        still holds in paper trading. Replay and Keep only the winners test all of them."""
        from ..market.pairs import configured_symbols, open_paper_symbols

        mkt = "futures" if futures_mode() else "spot"
        try:
            held = open_paper_symbols(paper_state, mkt) if paper_state else []
            return configured_symbols(settings.symbols, market=mkt, keep=held)
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
            base = replace(base, min_rr=g["min_rr"], max_rr=g["max_rr"], split_tp=g["split_tp"],
                           maker_entry=g["maker_entry"],
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

    def compute_all(tf: str, start: datetime, months: int, strategy: str) -> dict:
        """The same backtest on every pair of the workspace (its Markets pairs) at once: totals per
        strategy and per pair, and which pairs had no prices for the period."""
        per_pair, by_strat, names = [], {}, {}
        for sym in pairs():
            res = compute(sym, tf, start, months, strategy)
            names.update({x["id"]: x["name"] for x in res["strategies"]})
            closed = [t for t in res["trades"] if t.get("exit_ts")]
            wins = sum(1 for t in closed if t["r"] > 0)  # plain ints: the trades may hold numpy values
            per_pair.append({"symbol": sym, "candles": len(res["candles"]), "trades": len(closed), "wins": wins,
                             "total_r": round(float(sum(t["r"] for t in closed)), 2), "open": len(res["trades"]) - len(closed),
                             "fee_skipped": int(res.get("fee_skipped", 0)),
                             "strategies": len(res["strategies"])})
            for t in closed:
                b = by_strat.setdefault(t["strategy"], {"id": t["strategy"], "trades": 0, "wins": 0, "total_r": 0.0,
                                                        "pairs": set()})
                b["trades"] += 1
                b["wins"] += int(t["r"] > 0)
                b["total_r"] += float(t["r"])
                b["pairs"].add(sym)
        strategies = sorted(({**b, "name": names.get(b["id"], b["id"]), "total_r": round(b["total_r"], 2),
                              "pairs": len(b["pairs"])} for b in by_strat.values()),
                            key=lambda b: b["total_r"], reverse=True)
        return {"timeframe": tf, "start": int(start.timestamp() * 1000), "months": months, "pairs": per_pair,
                "strategies": strategies, "missing": [p["symbol"] for p in per_pair if not p["candles"]]}

    @app.get("/api/replay/all")
    async def api_replay_all(request: Request, tf: str = "1h", month: str = "", months: int = 1,
                             strategy: str = ""):
        _user, _token, redirect = guard(request)
        if redirect:
            return JSONResponse({"error": "sign in"}, status_code=401)
        start = _month(month)
        if tf not in REPLAY_TIMEFRAMES or start is None:
            return JSONResponse({"error": "pick a timeframe and a month"}, status_code=400)
        if start > datetime.now(timezone.utc):
            return JSONResponse({"error": "that month hasn't happened yet"}, status_code=400)
        months = min(max(int(months or 1), 1), MAX_MONTHS)
        return JSONResponse(await asyncio.to_thread(compute_all, tf, start, months, strategy))

    @app.post("/replay/download-all")
    def download_all(request: Request, csrf: str = Form(""), tf: str = Form("1h"), month: str = Form(""),
                     months: int = Form(1), strategy: str = Form("")):
        """Download the period for every workspace pair that has no prices for it yet, in one job."""
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        start = _month(month)
        if tf not in REPLAY_TIMEFRAMES or start is None:
            return RedirectResponse("/replay?error=" + _q("pick a timeframe and a month"), status_code=303)
        months = min(max(int(months or 1), 1), MAX_MONTHS)
        since = (start - timedelta(milliseconds=WARMUP_BARS * TIMEFRAME_MS[tf])).strftime("%Y-%m-%d")
        until = min(_add_months(start, months), datetime.now(timezone.utc),
                    start + timedelta(milliseconds=MAX_BARS * TIMEFRAME_MS[tf])).strftime("%Y-%m-%d")
        sym_args = [a for sym in pairs() for a in ("--symbol", sym)]
        jobs.submit(f"Download {tf} for every {ws()} pair from {month}",
                    [["download", "--since", since, "--until", until, "--market", ws(), "--exchange", exchange,
                      "--timeframe", tf, *sym_args]],
                    meta={"kind": "download", "by": user, "market": ws()})
        cache.clear()
        q = f"symbol=*&tf={_q(tf)}&month={_q(month)}&months={months}&strategy={_q(strategy)}"
        return RedirectResponse(f"/replay?{q}&msg=" + _q(f"Downloading {tf} prices for all {len(pairs())} pairs. "
                                                          "Press Load again when it is done (see Recent jobs on "
                                                          "the Overview page)."), status_code=303)

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
            first = store.first_ts(exchange, meta["symbol"], meta["tf"]) if not have else None
        finally:
            store.close()
        out = download_status(job["status"], have, meta["since_ms"], meta["until_ms"], meta["tf"],
                              job.get("error", ""), ahead)
        if first is not None and first >= meta["until_ms"]:  # nothing then: the coin was listed later
            out["first"] = first
            out["first_text"] = clock.local(first / 1000)[:10]
            out["first_month"] = datetime.fromtimestamp(first / 1000, timezone.utc).strftime("%Y-%m")
        return JSONResponse(out)

    def chosen_ids(strategy: str, tf: str) -> list[str] | None:
        """The strategies the Replay page's Strategy choice loads on ``tf`` (None: all of them)."""
        from ..learn.registry import make_strategy

        n = _top(strategy)
        if not n:
            return [strategy] if strategy and strategy != "win" and not strategy.startswith("top:") else None
        mkt = "futures" if futures_mode() else "spot"
        entries = [e for e in known_entries(Registry(registry_path).entries) if picks.trades_now(e, False, mkt)
                   and not picks.is_copy(e) and (futures_mode() or e.kind != "short")]
        by_id = {e.id: e for e in entries}
        out = []
        for i in best_ids(entries, tf)[0]:
            try:
                if i in by_id and tf in make_strategy(by_id[i]).spec.timeframes:
                    out.append(i)
            except Exception:
                continue
            if len(out) >= n:
                break
        return out

    @app.post("/replay/pick")
    def pick(request: Request, csrf: str = Form(""), tf: str = Form("4h"), undo: str = Form(""),
             month: str = Form(""), months: int = Form(0), strategy: str = Form(""),
             min_trades: int = Form(picks.MIN_TRADES)):
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
        mkt = "futures" if futures_mode() else "spot"
        where = "Futures AI" if futures_mode() else "Spot AI"
        if undo:
            jobs.submit(f"Undo the {where} {tf} pick", [["learn", "pick-timeframe", tf, "--clear", "--market", mkt]],
                        meta={"kind": "pick", "by": user})
            msg = f"Undoing the {where} {tf} pick: every strategy may trade {tf} on {where} again."
            cache.clear()
            return RedirectResponse(f"/replay?tf={_q(tf)}&msg=" + _q(msg), status_code=303)
        # the period loaded on the Replay page (From month and Length), not a fixed history
        now = datetime.now(timezone.utc)
        start = _month(month) or datetime(now.year - 1, now.month, 1, tzinfo=timezone.utc)
        months = min(max(int(months or 1), 1), MAX_MONTHS)
        until = min(_add_months(start, months), now)
        if start >= now:
            return RedirectResponse("/replay?error=" + _q("choose a From month in the past"), status_code=303)
        period = f"{start:%b %Y} to {until:%b %Y}" if until - start > timedelta(days=31) else f"{start:%b %Y}"
        sym_args = [a for sym in pairs() for a in ("--symbol", sym)]  # the workspace's own pairs
        if not sym_args:
            return RedirectResponse("/replay?error=" + _q(f"{where} has no pairs to test yet"), status_code=303)
        n = _top(strategy)
        label = (f"the best {n} strategies" if n else
                 "all strategies" if strategy in ("", "win") or strategy.startswith("top:") else "the chosen strategy")

        fewest = min_trades if min_trades in MIN_TRADE_CHOICES else picks.MIN_TRADES

        def start_for(t: str) -> datetime:
            """The loaded period's start, moved back so ``t`` gets at least its minimum months."""
            return _add_months(start, -max(0, MIN_PICK_MONTHS.get(t, 1) - months))

        def period_for(t: str) -> str:
            s0 = start_for(t)
            return f"{s0:%b %Y} to {until:%b %Y}" if until - s0 > timedelta(days=31) else f"{s0:%b %Y}"

        longer = {t: n for t, n in MIN_PICK_MONTHS.items() if n > months}
        if tf != "all":
            longer = {t: n for t, n in longer.items() if t == tf}
        stretched = (" " + " ".join(f"{t} is tested over {n} months ({period_for(t)}), not {months}, so its "
                                    "strategies have enough candles to make trades." for t, n in longer.items())
                     if longer else "")

        def steps_for(t: str, skip_empty: bool) -> list[list[str]]:
            t_start = start_for(t)
            warm = t_start - timedelta(milliseconds=picks.MIN_BARS * TIMEFRAME_MS[t])  # indicators warm up first
            only = chosen_ids(strategy, t)
            pick_args = ["learn", *sym_args, "pick-timeframe", t, "--since", f"{t_start:%Y-%m-%d}",
                         "--until", f"{until:%Y-%m-%d}", "--min-trades", str(fewest), "--market", mkt]
            if only is not None:
                pick_args += ["--only", ",".join(only)]
            if skip_empty:
                pick_args.append("--skip-empty")
            return [["download", "--since", f"{warm:%Y-%m-%d}", "--until", f"{until:%Y-%m-%d}", "--market", mkt,
                     "--exchange", exchange, "--timeframe", t, *sym_args], pick_args]

        if tf == "all":
            steps = [st for t in PICK_ALL_ORDER for st in steps_for(t, True)]  # quick timeframes first, 5m last
            jobs.submit(f"Keep only the winners on every timeframe on {where} pairs ({period})", steps,
                        meta={"kind": "pick", "by": user, "market": mkt})
            msg = (f"Testing {label} on each timeframe ({', '.join(PICK_ALL_ORDER)}) over {period} for the "
                   f"{len(pairs())} {where} pairs. On each timeframe the winners become {where}'s WIN strategies "
                   "and the rest stop trading it there; the other engine is not changed. Each timeframe's result "
                   "appears here as soon as it is done." + stretched)
            tf = "1h"
        else:
            period = period_for(tf)
            jobs.submit(f"Keep only the {tf} winners on {where} pairs ({period})", steps_for(tf, False),
                        meta={"kind": "pick", "by": user, "market": mkt})
            msg = (f"Testing {label} on {tf} over {period} for the {len(pairs())} {where} pairs. The winners keep "
                   f"trading {tf} on {where} only, as its new WIN strategies; the rest stop trading {tf} there, and "
                   "the other engine is not changed. This page shows the list when it's done (reload it in a few "
                   "minutes)." + stretched)
        cache.clear()
        q = f"tf={_q(tf)}&month={_q(f'{start:%Y-%m}')}&months={months}&strategy={_q(strategy)}"
        return RedirectResponse(f"/replay?{q}&msg=" + _q(msg), status_code=303)

    @app.post("/replay/pick/choose")
    async def pick_choose(request: Request):
        """The admin's own choice from a pick's tested strategies: keep exactly the ticked ones."""
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        form = await request.form()
        if not auth.check_csrf(token, str(form.get("csrf", ""))):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        tf = str(form.get("tf", ""))
        back = f"/replay?tf={_q(tf)}&month={_q(str(form.get('month', '')))}&months={_q(str(form.get('months', '1')))}" \
               f"&strategy={_q(str(form.get('strategy', '')))}"
        cur = picks.load(picks_file()).get(tf) if tf in REPLAY_TIMEFRAMES else None
        if not cur or not cur.get("results"):
            return RedirectResponse(back + "&error=" + _q("run Keep only the winners on this timeframe first"),
                                    status_code=303)
        if any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "pick"
               for j in jobs.list()):
            return RedirectResponse(back + "&error=" + _q("A pick is already running; see Recent jobs on the "
                                                          "Overview page."), status_code=303)
        tested = {r["id"] for r in cur["results"]}
        ids = [i for i in dict.fromkeys(str(v) for v in form.getlist("keep")) if i in tested]
        mkt = "futures" if futures_mode() else "spot"
        where = "Futures AI" if futures_mode() else "Spot AI"
        jobs.submit(f"Keep your {len(ids)} chosen {tf} strategies on {where}",
                    [["learn", "pick-timeframe", tf, f"--choose={','.join(ids)}", "--market", mkt, "--by", user]],
                    meta={"kind": "pick", "by": user, "market": mkt})
        cache.clear()
        msg = (f"Keeping the {len(ids)} strategies you ticked as {where}'s WIN strategies on {tf}; the others stop "
               f"trading {tf} on {where}. Reload in a few seconds." if ids else
               f"You ticked none, so no strategy will trade {tf} on {where}. Undo the {tf} pick to let them all "
               "trade it again.")
        return RedirectResponse(back + "&msg=" + _q(msg), status_code=303)

    def pick_card(tf: str, token: str, month: str = "", months: int = 1, strategy: str = "") -> str:
        p = picks.load(picks_file())
        cur = p.get(tf)
        others = ", ".join(t for t in REPLAY_TIMEFRAMES if t in p and t != tf)
        period = (f'<input type="hidden" name="month" value="{_e(month)}"><input type="hidden" name="months" '
                  f'value="{months}"><input type="hidden" name="strategy" value="{_e(strategy)}">')
        opts = "".join(f'<option value="{t}"{" selected" if t == tf else ""}>{t}</option>' for t in REPLAY_TIMEFRAMES)
        fewest = "".join(f'<option value="{n}"{" selected" if n == picks.MIN_TRADES else ""}>{n}</option>'
                         for n in MIN_TRADE_CHOICES)
        head = (f'{_e(tf)} pick on: {len(cur.get("picked", []))} strategies kept'
                f'{" (your choice)" if cur.get("hand") else ""}' if cur
                else "Keep only the winners on one timeframe")
        out = (f'<details class="card" id="tf-pick" data-tf="{_e(tf)}"><summary><b>{head}</b></summary>'
               '<p class="small muted">Tests the strategies loaded above (the Strategy choice) on the chosen '
               'timeframe over the period loaded above (From month and Length), for this workspace\'s pairs only (in '
               'the Futures view the Futures AI pairs, in the Spot view the Spot AI pairs), after fees. A strategy is '
               'kept when its total is above 0R over at least the "Fewest trades" (10 unless you change it) and '
               'it made money in at least as many 3-month quarters as it lost in, so one lucky quarter is not '
               'enough. Kept strategies become this engine\'s WIN strategies on that timeframe; the others stop '
               'trading it on this engine (their other timeframes carry on). Each engine has its own picks. '
               'On slow charts a pick tests at least ' + ", ".join(f"{n} months on {t}" for t, n in MIN_PICK_MONTHS.items())
               + ' (starting earlier when less is loaded), because one month has too few candles for 10 trades. '
               'After a pick, "Choose which to keep" shows every tested strategy\'s full results so you can tick '
               'your own, including ones with fewer trades.</p>'
               f'<form method="post" action="/replay/pick" class="row pick-form">{_csrf(auth, token)}' + period +
               f'<label>Timeframe <select name="tf">{opts}</select></label>'
               f'<label>Fewest trades <select name="min_trades">{fewest}</select></label>'
               '<button>Keep only the winners</button></form>'
               f'<form method="post" action="/replay/pick" class="row pick-form">{_csrf(auth, token)}' + period +
               f'<input type="hidden" name="tf" value="all"><input type="hidden" name="min_trades" '
               f'value="{picks.MIN_TRADES}">'
               '<button class="ghost">Keep the winners on every timeframe</button></form>')
        if others:
            out += f'<p class="small">Also picked: {_e(others)}.</p>'
        if cur:
            won = [r for r in cur.get("results", []) if r["id"] in set(cur.get("picked", []))]
            when = clock.local(cur["at"] / 1000)
            tested = (f'{clock.local(cur["since"] / 1000)[:10]} to {clock.local(cur["until"] / 1000)[:10]}'
                      if cur.get("until") and cur.get("since") else f'the last {cur.get("years", 2):g} years')
            out += (f'<p><b>{_e(tf)} pick ({_e(when)}):</b> {len(won)} of {len(cur.get("results", []))} '
                    f'strategies kept, tested {_e(tested)} on {len(cur.get("pairs", []))} pairs: '
                    f'{_e(", ".join(cur.get("pairs", [])))}.</p>')
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
            out += _tested_table(cur, tf, _csrf(auth, token) + period)
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
        symbol = symbol if symbol in symbols or (symbol == "*" and symbols) else (symbols[0] if symbols else "")
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
            f'<label>Pair <select name="symbol">{options([("*", f"All {len(symbols)} pairs (totals)")] + [(s, s) for s in symbols], symbol)}</select></label>'
            f'<label>Timeframe <select name="tf">{options([(t, t) for t in REPLAY_TIMEFRAMES], tf)}</select></label>'
            f'<label>From month <input type="month" name="month" value="{_e(month)}" '
            f'max="{now.year}-{now.month:02d}"></label>'
            f'<label>Length <select name="months">{options(LENGTHS, str(months))}'
            '</select></label>'
            f'<label class="wide">Strategy <select name="strategy">{strat_opts}</select></label>'
            '<button type="submit">Load</button></form>'
            '<div id="replay-msg" class="muted"></div>'
            + pick_card(tf, token, month, months, strategy) +
            f'<form id="replay-download-all" method="post" action="/replay/download-all" class="row hidden">'
            f'{_csrf(auth, token)}<input type="hidden" name="tf"><input type="hidden" name="month">'
            '<input type="hidden" name="months"><input type="hidden" name="strategy">'
            '<button>Download the missing prices from Bybit</button></form>'
            f'<form id="replay-download" method="post" action="/replay/download" class="row hidden">{_csrf(auth, token)}'
            '<input type="hidden" name="symbol"><input type="hidden" name="tf"><input type="hidden" name="month">'
            '<input type="hidden" name="months"><button>Download these prices from Bybit</button></form>'
            '<div id="replay-progress" class="card hidden" role="status" aria-live="polite">'
            '<p id="progress-text" class="small"></p>'
            '<div class="bar progress"><span id="progress-fill" style="width:0%"></span></div></div>'
            '<div id="replay-all" class="card hidden"></div>'
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
            '<div id="replay-detail"><div class="stats" id="replay-stats"></div>'
            '<h2>Strategies so far</h2><p class="muted small">Closed trades up to the current candle, best first. '
            'Win % is the share of trades that made money after fees and slippage; R is the amount risked per '
            'trade.</p><div id="board"></div>'
            '<h2>Trades</h2><div id="log"></div></div></div>'
            '<noscript><p class="bad">The replay needs JavaScript.</p></noscript>'
            '<p class="muted small">After Load, the indicators the chosen strategies use switch on (the number on '
            'a switch is how many of them use it); tap a switch to show or hide one. Indicators use the strategies\' '
            'own formulas and only the candles up to the current one: support/resistance from the last 300, '
            'Fibonacci and the volume profile from the last 100. Triangles mark entries (up: buy, down: short), '
            'dots the exits (green: profit, red: loss).</p>'
            '<script src="/static/chartstudies.js" defer></script><script src="/static/replay.js" defer></script>')
        return page("Replay backtest", body, user, token)


def _tested_table(cur: dict, tf: str, hidden: str = "") -> str:
    """Every strategy the pick tested, best first, with its full results and why the rules would or
    wouldn't keep it, as a form: the admin ticks which to keep (the kept ones start ticked), and how
    much price history each pair had."""
    results = cur.get("results") or []
    if not results:
        return ""
    kept = set(cur.get("picked", []))
    fewest = cur.get("min_trades", picks.MIN_TRADES)

    def rule(r: dict) -> str:
        why = picks.why_not(r, fewest)
        return '<span class="good">passes the rules</span>' if not why else _e(why)

    def quarters(r: dict) -> str:
        qs = r.get("quarters") or {}
        return ", ".join(f'{_e(q)} <span class="{"good" if v > 0 else "bad"}">{v:+.2f}R</span>'
                         for q, v in qs.items()) or "no trades"

    rows = "".join(
        f'<tr><td style="min-width:10em"><label class="keep-tick"><input type="checkbox" name="keep" value="{_e(r["id"])}" data-r="{r["total_r"]}"'
        f'{" checked" if r["id"] in kept else ""}> {_e(r["name"])}</label></td>'
        f'<td>{r["trades"]}</td><td>{r["win_rate"]:.0f}%</td>'
        f'<td class="{"good" if r["total_r"] > 0 else "bad"}">{r["total_r"]:+.2f}R</td>'
        f'<td>{(r["total_r"] / r["trades"] if r["trades"] else 0):+.2f}R</td>'
        f'<td>{r["quarters_up"]} of {r["quarters_up"] + r["quarters_down"]}</td>'
        f'<td class="small">{quarters(r)}</td><td class="small">{rule(r)}'
        f'{" · few trades, could be luck" if 0 < r["trades"] < picks.MIN_TRADES else ""}</td></tr>'
        for r in sorted(results, key=lambda r: r["total_r"], reverse=True))
    rule_ids = cur.get("rule_picked") if cur.get("hand") else None
    note = (f'<p class="small">You chose these by hand{(" (" + _e(cur["by"]) + ")") if cur.get("by") else ""}; '
            f'the rules alone would keep {len(rule_ids)}.</p>' if rule_ids is not None else "")
    out = (f'<details open><summary><b>Choose which to keep ({len(results)} tested)</b></summary>'
           '<p class="small muted">Every strategy the pick tested, best total first, after fees. Tick the ones you '
           f'want {_e(tf)} WIN strategies on this engine, even ones the rules skip (for example fewer than '
           f'{fewest} trades), then press Keep the ticked strategies. No new test runs; the others stop trading '
           f'{_e(tf)} on this engine. Few trades means the result could be luck: a strategy with 2 winning trades '
           'has shown very little.</p>' + note +
           f'<form method="post" action="/replay/pick/choose" class="pick-choose">{hidden}'
           f'<input type="hidden" name="tf" value="{_e(tf)}">'
           '<div class="row"><button type="button" class="ghost" data-tick="all">Tick all</button>'
           '<button type="button" class="ghost" data-tick="none">Tick none</button>'
           '<button type="button" class="ghost" data-tick="profit">Tick all in profit</button></div>'
           '<table><tr><th>Strategy</th><th>Trades</th><th>Win %</th><th>Total</th>'
           '<th>Per trade</th><th>Quarters up</th><th style="min-width:11em">Each quarter</th>'
           '<th style="min-width:15em">Rules</th></tr>'
           f'{rows}</table><button>Keep the ticked strategies</button></form>')
    bars = cur.get("bars") or {}
    step = TIMEFRAME_MS.get(tf)
    if bars and step and cur.get("since") and cur.get("at"):
        full = max(1, (cur["at"] - cur["since"]) // step)
        out += ('<p class="small muted">Price history tested: ' + ", ".join(
            f'{_e(sym)} {n:,} candles ({min(100, 100 * n // full)}%)' for sym, n in bars.items())
            + '. A coin listed recently has less.</p>')
    return out + '</details>'
