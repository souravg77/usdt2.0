"""The Monitor page: feed health, derivatives, the signal and abstention stream, and risk and
exposure, each also served as JSON under ``/api/monitor/...``.

- Feed health: the live executor's price stream (``data/ops/feed.json``, written every few
  seconds: connection, ping round trip, reconnects, age of each pair's last update) and missing
  candles in the collector's database over the last 7 days.
- Derivatives: last, mark and index price, basis and funding of each pair's USDT perpetual, from
  Bybit's public API (cached a few seconds, no key).
- Signals: agents A-G and every strategy on each pair's newest closed 1h candle: the candidate
  signals and, for every strategy that didn't signal, why (``agents.analyse``).
- Risk: open live positions, today's realised loss against the 1% daily cap, margin used by
  shorts, and a one-day 95% historical value at risk of the open positions. The kill switch
  posts to the same ``/trading/kill`` as the Trading page (type KILL to confirm).

Read only, apart from the kill switch form. Nothing here can open a trade.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import EXCHANGE
from ..data.store import CandleStore
from ..models import TIMEFRAME_MS
from ..ops import feedstats
from ..clock import local

DAY_MS = 86_400_000
GAP_DAYS = 7
SIGNAL_TF = "1h"
SIGNAL_BARS = 600
SIGNAL_PAIRS = 6  # the board runs every strategy, so only the first few pairs
FUTURES_TOP = 10  # the Futures view: the top 10 perpetual gainers
FEED_STALE_S = 30.0
VAR_DAYS = 90


def _num(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


# ---- feed health --------------------------------------------------------------------------------


def candle_gaps(db_path: str, exchange: str, symbols: list[str], timeframes: list[str], now_ms: int) -> list[dict]:
    """Missing candles per pair and timeframe over the last ``GAP_DAYS`` days, and how old the
    newest stored candle is."""
    try:
        store = CandleStore(db_path)
    except Exception:
        return []
    rows = []
    try:
        for sym in symbols:
            for tf in timeframes:
                step = TIMEFRAME_MS.get(tf)
                if not step:
                    continue
                start = now_ms - GAP_DAYS * DAY_MS
                ts = [c.ts for c in store.load(exchange, sym, tf, start)]
                if not ts:
                    continue
                expected = (ts[-1] - ts[0]) // step + 1
                rows.append({"symbol": sym, "timeframe": tf, "candles": len(ts),
                             "missing": int(max(0, expected - len(ts))),
                             "newest_age_s": round((now_ms - (ts[-1] + step)) / 1000)})
    finally:
        store.close()
    return rows


def feed_health(path: str, db_path: str, exchange: str, symbols: list[str], timeframes: list[str],
                now: float) -> dict:
    st = feedstats.load(path)
    age = now - st["written"] if st.get("written") else None
    if not st:
        state = "not running (the live executor writes this; it only runs once live is unlocked)"
    elif age is not None and age > FEED_STALE_S:
        state = f"stale: last written {age:.0f}s ago (is executor-live running?)"
    else:
        state = "connected" if st.get("connected") else "disconnected"
    quiet = sorted(s for s, a in (st.get("ages") or {}).items() if a is None or a > 15)
    return {"state": state, "written_age_s": None if age is None else round(age, 1),
            "latency_ms": st.get("latency_ms"), "reconnects": st.get("reconnects"), "last_error": st.get("last_error"),
            "ages": st.get("ages") or {}, "quiet_pairs": quiet, "history": (st.get("history") or [])[-10:],
            "gaps": candle_gaps(db_path, exchange, symbols, timeframes, int(now * 1000))}


# ---- derivatives --------------------------------------------------------------------------------


class PerpMarket:
    """Bybit USDT-perpetual tickers for the dashboard (public, keyless, cached)."""

    def __init__(self, client=None, cache_s: float = 5.0, clock=time.monotonic):
        self._client, self.cache_s, self._clock = client, cache_s, clock
        self._hit: tuple[float, tuple, dict] | None = None
        self._lock = asyncio.Lock()

    def client(self):
        if self._client is None:
            import ccxt.async_support as ccxt_async

            self._client = ccxt_async.bybit({"enableRateLimit": True,
                                             "options": {"defaultType": "swap", "fetchMarkets": {"types": ["linear"]}}})
        return self._client

    async def snapshot(self, symbols: list[str]) -> dict:
        from ..data.context import perp_symbol
        from ..data.derivs import parse_ticker

        key = tuple(symbols)
        async with self._lock:
            if self._hit and self._hit[1] == key and self._clock() - self._hit[0] < self.cache_s:
                return self._hit[2]
            try:
                # only pairs Bybit lists a USDT perpetual for: one without would fail the whole request
                markets = await asyncio.wait_for(self.client().load_markets(), timeout=8)
                listed = [perp_symbol(s) for s in symbols if perp_symbol(s) in markets]
                raw = await asyncio.wait_for(self.client().fetch_tickers(listed), timeout=8) if listed else {}
                rows = []
                for s in symbols:
                    t = raw.get(perp_symbol(s))
                    if not t:
                        continue
                    d = parse_ticker(t.get("info") or {}, s)
                    if d.last is None:
                        d.last = _num(t.get("last"))
                    rows.append({**d.as_dict(), "funding_in_h": None if d.funding_in_s() is None
                                 else round(d.funding_in_s() / 3600, 2)})
                out = {"source": "live", "rows": rows}
            except Exception as exc:
                out = {"source": "unavailable", "rows": [], "note": f"Bybit perpetuals not reachable ({type(exc).__name__})"}
            self._hit = (self._clock(), key, out)
            return out

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()


# ---- signals ------------------------------------------------------------------------------------


class SignalBoards:
    """``agents.analyse`` per pair on the newest closed candles, recomputed when a candle closes."""

    def __init__(self, db_path: str, exchange: str = EXCHANGE, strategies=None, context_db: str | None = None):
        # ``db_path``: the candles (Futures view: the perpetuals' database); funding and open
        # interest stay in the main database (``context_db``)
        self.db_path, self.exchange, self.strategies = db_path, exchange, strategies
        self.context_db = context_db or db_path
        self._memo: dict[str, tuple[int, dict]] = {}

    def frame(self, symbol: str, now_ms: int) -> pd.DataFrame:
        from ..data.context import ContextStore, attach_context, has_context
        from ..indicators import candles_to_frame
        from ..indicators.results import closed_only

        store = CandleStore(self.db_path)
        try:
            step = TIMEFRAME_MS[SIGNAL_TF]
            candles = store.load(self.exchange, symbol, SIGNAL_TF, now_ms - SIGNAL_BARS * step)
            df = closed_only(candles_to_frame(candles), now_ms, step) if candles else pd.DataFrame()
            ctx_db = CandleStore(self.context_db) if self.context_db != self.db_path else store
            try:
                with_ctx = len(df) and has_context(ctx_db)
            finally:
                if ctx_db is not store:
                    ctx_db.close()
            if with_ctx:
                ctx = ContextStore(self.context_db)
                try:
                    df = attach_context(df, ctx, self.exchange, symbol, SIGNAL_TF)
                finally:
                    ctx.close()
            return df
        finally:
            store.close()

    def board(self, symbol: str, now_ms: int) -> dict:
        from ..agents import analyse

        df = self.frame(symbol, now_ms)
        if not len(df):
            return {"symbol": symbol, "note": "no stored 1h candles (is the collector running?)",
                    "candidates": [], "abstentions": []}
        newest = int(df["ts"].iloc[-1])
        hit = self._memo.get(symbol)
        if hit and hit[0] == newest:
            return hit[1]
        b = analyse(df, self.exchange, symbol, SIGNAL_TF, strategies=self.strategies, now_ms=now_ms).as_dict()
        g, c = b["messages"]["G"], b["messages"]["C"]
        out = {"symbol": symbol, "bar": newest, "regime": g["regime"], "regime_reason": g["reason"],
               "no_trade": g["no_trade"], "trend": c["direction"], "market_ok": not b["messages"]["A"]["problems"],
               "problems": b["messages"]["A"]["problems"], "candidates": b["candidates"],
               "abstentions": b["abstentions"]}
        self._memo[symbol] = (newest, out)
        return out


# ---- risk ---------------------------------------------------------------------------------------


def _day_start_ms(now: float) -> int:
    d = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def value_at_risk(positions: list[dict], db_path: str, exchange: str, now_ms: int,
                  level: float = 0.95) -> float | None:
    """One-day historical value at risk (USDT) of ``positions`` (symbol, notional, short): the
    loss exceeded on only ``1 - level`` of the last ``VAR_DAYS`` days' 24-hour moves, all pairs
    moving as they did together on each day."""
    if not positions:
        return 0.0
    try:
        store = CandleStore(db_path)
    except Exception:
        return None
    try:
        rets = {}
        for sym in {p["symbol"] for p in positions}:
            c = store.load(exchange, sym, "1h", now_ms - VAR_DAYS * DAY_MS)
            if len(c) < 48:
                return None
            s = pd.Series([x.close for x in c], index=[x.ts for x in c])
            rets[sym] = s.pct_change(24).dropna()
    finally:
        store.close()
    frame = pd.DataFrame(rets).dropna()
    if len(frame) < 24:
        return None
    pnl = np.zeros(len(frame))
    for p in positions:
        sign = -1.0 if p.get("short") else 1.0
        pnl += sign * float(p["notional"]) * frame[p["symbol"]].to_numpy()
    return round(float(-np.quantile(pnl, 1 - level)), 2)


def risk_view(live_state: dict, paper_state: dict, db_path: str, exchange: str, limits, now: float) -> dict:
    from ..trade import futures

    positions = []
    for p in (live_state.get("positions") or {}).values():
        if p.get("status") not in ("open", "pending_exit"):
            continue
        short = p.get("side") == "short"
        notional = float(p.get("notional") or p.get("cost") or 0.0)
        positions.append({"symbol": p.get("symbol"), "short": short, "notional": round(notional, 2),
                          "leverage": float(p.get("leverage") or 1.0), "risk_usdt": p.get("risk_usdt"),
                          "stop": p.get("stop"), "stop_armed": bool(p.get("stop_armed"))})
    equity = float(live_state.get("last_equity") or 0.0)
    day0 = _day_start_ms(now)
    today = sum(float(t.get("pnl_usdt") or 0) for t in live_state.get("closed") or [] if (t.get("exit_ts") or 0) >= day0)
    start = equity - today
    cap = limits.daily_loss_pct * start if start > 0 else None
    margin = sum(futures.margin(p["notional"], p["leverage"]) for p in positions if p["short"])
    exposure = sum(p["notional"] for p in positions)
    var = value_at_risk(positions, db_path, exchange, int(now * 1000)) if positions else 0.0
    return {
        "live": bool(live_state), "equity": round(equity, 2) if equity else None, "positions": positions,
        "exposure_usdt": round(exposure, 2), "exposure_pct": round(exposure / equity, 4) if equity else None,
        "today_pnl": round(today, 2), "daily_cap_usdt": None if cap is None else round(cap, 2),
        "daily_cap_used": round(max(0.0, -today) / cap, 4) if cap else 0.0,
        "margin_usdt": round(margin, 2), "margin_used": round(margin / equity, 4) if equity else None,
        "var95_usdt": var, "var95_pct": round(var / equity, 4) if equity and var is not None else None,
        "paper_open": len((paper_state or {}).get("open") or {}),
        "limits": {"risk_per_trade": limits.risk_per_trade, "daily_loss_pct": limits.daily_loss_pct,
                   "max_open_positions": limits.max_open_positions, "max_leverage": futures.MAX_LEVERAGE},
    }


# ---- routes -------------------------------------------------------------------------------------


def add_monitor_routes(app, *, settings, page, guard, auth, paths, csrf, control, clock=time.time,
                       perp: PerpMarket | None = None, boards: SignalBoards | None = None,
                       exchange: str = EXCHANGE) -> None:
    from .app import _e, _load_json

    perp = perp or PerpMarket()
    from ..data.perp import db_for, futures_db_path

    # each view its own candles: the Futures view reads only the perpetuals' database
    fboards = boards or SignalBoards(futures_db_path(settings.db_path), exchange, context_db=settings.db_path)
    boards = boards or SignalBoards(settings.db_path, exchange)
    feed_path = getattr(paths, "feed", feedstats.DEFAULT_PATH)
    paper_state = getattr(paths, "paper_state", "")

    def futures_view() -> bool:
        from ..learn import wallet

        try:
            return bool(paper_state) and wallet.futures_on(wallet.market_path(paper_state))
        except Exception:
            return False

    def workspace_symbols() -> list[str]:
        """The Futures view: the top 10 perpetual gainers (none while that list is off). The Spot
        view: Spot AI's pairs."""
        from ..market import gainers
        from ..market.pairs import configured_symbols

        try:
            if futures_view():
                return gainers.symbols(os.environ.get("AITRADER_GAINERS_FILE") or gainers.DEFAULT_PATH)[:FUTURES_TOP]
            return configured_symbols(list(settings.symbols), market="spot")
        except Exception:
            return [] if futures_view() else list(settings.symbols)

    def api_guard(request: Request):
        _u, _t, redirect = guard(request)
        return JSONResponse({"error": "sign in"}, status_code=401) if redirect else None

    def feed_data():
        db = db_for(settings.db_path, "futures" if futures_view() else "spot")
        return feed_health(feed_path, db, exchange, workspace_symbols(), list(settings.timeframes), clock())

    def risk_data():
        from ..trade.risk import RiskLimits

        live = _load_json(os.path.join(paths.trade_dir, "live_state.json"))
        return risk_view(live, _load_json(paths.paper_state), settings.db_path, exchange, RiskLimits.from_env(), clock())

    def signal_data():
        now_ms = int(clock() * 1000)
        syms = workspace_symbols()
        if futures_view():
            return [fboards.board(s, now_ms) for s in syms[:FUTURES_TOP]]
        return [boards.board(s, now_ms) for s in syms[:SIGNAL_PAIRS]]

    @app.get("/api/monitor/feed")
    def api_feed(request: Request):
        return api_guard(request) or JSONResponse(feed_data())

    @app.get("/api/monitor/derivatives")
    async def api_derivs(request: Request):
        return api_guard(request) or JSONResponse(await perp.snapshot(workspace_symbols()))

    @app.get("/api/monitor/signals")
    async def api_signals(request: Request):
        return api_guard(request) or JSONResponse({"timeframe": SIGNAL_TF,
                                                   "boards": await asyncio.to_thread(signal_data)})

    @app.get("/api/monitor/risk")
    def api_risk(request: Request):
        return api_guard(request) or JSONResponse({**risk_data(), "control": control().state()})

    @app.get("/monitor")
    async def monitor(request: Request):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        syms = workspace_symbols()
        feed, derivs, risk = feed_data(), await perp.snapshot(syms), risk_data()
        sig = await asyncio.to_thread(signal_data)
        if futures_view():
            which = (f"Futures AI view: the top {len(syms)} perpetual gainers ({', '.join(syms)})." if syms else
                     "Futures AI view: the Futures top gainers list is off or out of date, so there are no "
                     "pairs to show. Turn it on on the Pairs page.")
        else:
            which = f"Spot AI view: Spot AI's pairs ({len(syms)})."
        body = [f'<p class="small muted">{_e(which)}</p>', '<div class="grid">', _feed_card(feed, _e), _risk_card(risk, control().state(), csrf(token), _e),
                "</div>", "<h2>Derivatives</h2>", _derivs_table(derivs, _e),
                "<h2>Signals and abstentions</h2>", _signals_html(sig, _e),
                '<p class="muted small">JSON: /api/monitor/feed · /api/monitor/derivatives · '
                "/api/monitor/signals · /api/monitor/risk</p>"]
        return page("Monitor", "".join(body), user, token, refresh=30)


def _pct(v, digits=2) -> str:
    return "-" if v is None else f"{v * 100:+.{digits}f}%"


def _feed_card(f: dict, e) -> str:
    rows = "".join(f"<tr><td>{e(g['symbol'])}</td><td>{e(g['timeframe'])}</td><td>{g['missing']}</td>"
                   f"<td>{g['newest_age_s']}s</td></tr>" for g in f["gaps"] if g["missing"]) or \
        '<tr><td colspan="4" class="muted">no missing candles in the last 7 days</td></tr>'
    hist = "".join(f"<li>{local(h['at'], '%H:%M:%S', suffix=False)} {e(h['event'])}</li>"
                   for h in reversed(f["history"])) or '<li class="muted">none yet</li>'
    quiet = ", ".join(f["quiet_pairs"]) or "none"
    return (f'<div class="card"><h3>Feed health</h3><p>Price stream: <b>{e(f["state"])}</b><br>'
            f'Ping round trip: {e(f["latency_ms"] if f["latency_ms"] is not None else "-")} ms · '
            f'reconnects: {e(f["reconnects"] if f["reconnects"] is not None else "-")}<br>'
            f'Pairs quiet for over 15 s: {e(quiet)}</p>'
            f'<table><tr><th>Pair</th><th>TF</th><th>Missing (7d)</th><th>Newest</th></tr>{rows}</table>'
            f'<p class="small">Connection log</p><ul class="small">{hist}</ul></div>')


def _risk_card(r: dict, c: dict, csrf: str, e) -> str:
    used = r["daily_cap_used"] or 0.0
    bar = (f'<div class="bar"><span style="width:{min(100, used * 100):.0f}%"></span></div>'
           f'<p class="small">Daily loss: {r["today_pnl"]:+.2f} USDT of the '
           f'{e(r["daily_cap_usdt"] if r["daily_cap_usdt"] is not None else "-")} USDT cap ({used:.0%} used)</p>')
    state = ("KILL SWITCH ON" if c.get("killed") else "SAFETY HALT" if c.get("halted")
             else "PAUSED" if c.get("paused") else "running")
    pos = "".join(f"<tr><td>{e(p['symbol'])}</td><td>{'short' if p['short'] else 'long'}</td>"
                  f"<td>{p['notional']:,.2f}</td><td>{'yes' if p['stop_armed'] else '<b class=bad>no</b>'}</td></tr>"
                  for p in r["positions"]) or '<tr><td colspan="4" class="muted">no open live positions</td></tr>'
    return (f'<div class="card"><h3>Risk and exposure</h3><p>Trading: <b>{e(state)}</b>'
            f'{" · live executor" if r["live"] else " · live executor not running"} · '
            f'{r["paper_open"]} paper position(s) open<br>'
            f'Equity: {e(r["equity"] if r["equity"] is not None else "-")} USDT · exposure '
            f'{_pct(r["exposure_pct"], 1)} · margin used {_pct(r["margin_used"], 1)}<br>'
            f'1-day 95% value at risk: {e(r["var95_usdt"] if r["var95_usdt"] is not None else "-")} USDT '
            f'({_pct(r["var95_pct"])})</p>{bar}'
            f'<table><tr><th>Pair</th><th>Side</th><th>Notional</th><th>Stop on Bybit</th></tr>{pos}</table>'
            f'<form method="post" action="/trading/kill" class="kill">{csrf}'
            '<label>Emergency kill switch: stops entries and closes everything the live executor opened. '
            'Type KILL to confirm <input name="confirm" autocomplete="off" autocapitalize="characters" autocorrect="off" spellcheck="false" required placeholder="KILL"></label>'
            '<button class="danger">Pull the kill switch</button></form></div>')


def _derivs_table(d: dict, e) -> str:
    if not d.get("rows"):
        return f'<p class="muted">{e(d.get("note") or "no perpetual data")}</p>'
    rows = "".join(
        f"<tr><td>{e(r['symbol'])}</td><td>{e(r['last'])}</td><td>{e(r['mark'])}</td><td>{e(r['index'])}</td>"
        f"<td>{_pct(r['basis'], 3)}</td><td>{_pct(r['funding_rate'], 4)}</td><td>{_pct(r['funding_annualized'], 1)}</td>"
        f"<td>{e(r['funding_in_h'] if r['funding_in_h'] is not None else '-')} h</td></tr>" for r in d["rows"])
    return ("<table><tr><th>Pair</th><th>Last</th><th>Mark</th><th>Index</th><th>Basis</th><th>Funding / 8h</th>"
            f"<th>Per year</th><th>Next in</th></tr>{rows}</table>")


def _signals_html(boards: list[dict], e) -> str:
    out = []
    for b in boards:
        if b.get("note"):
            out.append(f'<p><b>{e(b["symbol"])}</b>: <span class="muted">{e(b["note"])}</span></p>')
            continue
        cands = "".join(f"<tr><td>{e(c['strategy'])}</td><td>{e(c['side'])}</td><td>{c['entry']:.6g}</td>"
                        f"<td>{c['stop']:.6g}</td><td>{c['target']:.6g}</td><td>{c['confidence']:.2f}</td>"
                        f"<td>{e(c['reason'])}</td></tr>" for c in b["candidates"])
        reasons: dict[str, list[str]] = {}
        for a in b["abstentions"]:
            reasons.setdefault(a["reason"], []).append(a["strategy"])
        abst = "".join(f"<li>{e(why)}: <span class='muted'>{e(', '.join(names))}</span></li>"
                       for why, names in sorted(reasons.items(), key=lambda kv: -len(kv[1])))
        out.append(f'<div class="card"><h3>{e(b["symbol"])} · {e(b["regime"])}</h3>'
                   f'<p class="small">{e(b["regime_reason"])}</p>'
                   + (f"<table><tr><th>Strategy</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th>"
                      f"<th>Confidence</th><th>Why</th></tr>{cands}</table>" if cands
                      else '<p class="muted">no candidate signal on the newest closed candle</p>')
                   + f'<details><summary>Why the others abstained ({len(b["abstentions"])})</summary>'
                     f'<ul class="small">{abst}</ul></details></div>')
    return "".join(out) or '<p class="muted">no pairs configured</p>'
