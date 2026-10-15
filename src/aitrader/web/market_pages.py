"""The Markets page: live prices and a candlestick chart for each pair.

The page's script (``static/market.js``) asks this server every 1.5 seconds for fresh candles and
prices (``/api/market/...``), which come from Bybit's public API through ``LiveMarket``
(cached ~1s, no key needed). Without JavaScript the page shows the candles the ``collector``
service saved, drawn on the server.
"""

from __future__ import annotations

import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..config import EXCHANGE
from ..data.store import CandleStore
from ..market import gainers
from ..market.pairs import configured_symbols, open_paper_symbols
from .app import _e, _q
from .live import LiveMarket
from .studies import compute, paper_trades
from ..clock import label as tz_label, local

CHART_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000}
BARS = 120
STUDY_BARS = 250  # indicator values sent to the chart (it shows at most ~200 candles)
DAY_MS = 86_400_000
STATIC = os.path.join(os.path.dirname(__file__), "static")


def add_market_routes(app, *, settings, page, guard, exchange: str = EXCHANGE, clock=None,
                      live: LiveMarket | None = None, paper_state: str = "", registry: str = "") -> None:
    live = live or LiveMarket(settings.db_path, exchange)
    perp_live: list = []  # the Futures-mode feed, made on first use

    def futures_mode() -> bool:
        from ..learn import wallet

        return bool(paper_state) and wallet.futures_on(wallet.market_path(paper_state))

    def feed() -> LiveMarket:
        """Perpetual prices in Futures mode, spot otherwise."""
        if not futures_mode():
            return live
        if not perp_live:
            perp_live.append(LiveMarket(settings.db_path, exchange, perp=True))
        return perp_live[0]
    memo: dict[tuple, tuple[tuple, dict]] = {}  # (symbol, tf) -> (candle fingerprint, studies)
    tfs = CHART_TIMEFRAMES

    def held() -> list[str]:
        """Pairs the workspace's engine holds an open paper position in."""
        try:
            return open_paper_symbols(paper_state, "futures" if futures_mode() else "spot") if paper_state else []
        except Exception:
            return []

    def current() -> list[str]:
        """The workspace's pairs: what its engine trades now (in the Futures view its top gainers while
        that list is on) plus every pair it still holds an open paper position in."""
        mkt = "futures" if futures_mode() else "spot"
        try:
            return configured_symbols(settings.symbols, market=mkt, keep=held())
        except Exception:
            return list(settings.symbols)

    def pick(symbol: str, tf: str) -> tuple[str, str]:
        symbols = current()
        symbol = symbol if symbol in symbols else (symbols[0] if symbols else "")
        return symbol, tf if tf in tfs else "15m"

    def api_guard(request: Request):
        _user, _token, redirect = guard(request)
        return JSONResponse({"error": "sign in"}, status_code=401) if redirect else None

    @app.get("/api/market/candles")
    async def api_candles(request: Request, symbol: str = "", tf: str = "15m"):
        bad = api_guard(request)
        if bad:
            return bad
        symbol, tf = pick(symbol, tf)
        return JSONResponse({"symbol": symbol, "tf": tf, **await feed().candles(symbol, tf)})

    @app.get("/api/market/studies")
    async def api_studies(request: Request, symbol: str = "", tf: str = "15m"):
        """Indicators for the same candles /api/market/candles returns, plus the paper trader's
        trades on this pair. Recomputed only when a candle changed."""
        bad = api_guard(request)
        if bad:
            return bad
        symbol, tf = pick(symbol, tf)
        data = await feed().candles(symbol, tf)
        rows = data.get("candles") or []
        mark = (len(rows), tuple(rows[0]) if rows else (), tuple(rows[-1]) if rows else ())
        hit = memo.get((symbol, tf))
        if hit is None or hit[0] != mark:
            hit = memo[(symbol, tf)] = (mark, compute(rows, tail=STUDY_BARS))
        return JSONResponse({"symbol": symbol, "tf": tf, "ts": [r[0] for r in rows[-STUDY_BARS:]], **hit[1],
                             "trades": paper_trades(paper_state, symbol) if paper_state else {"open": [], "closed": []}})

    @app.get("/api/market/tickers")
    async def api_tickers(request: Request):
        bad = api_guard(request)
        if bad:
            return bad
        hot = set(gainers.symbols(gainers.path_for("futures" if futures_mode() else "spot")))
        mine = set(held())
        res = await feed().tickers(current())
        def mark(t: dict) -> dict:
            return {**t, **({"gainer": True} if t["symbol"] in hot else {}),
                    **({"held": True} if t["symbol"] in mine else {})}
        return JSONResponse({**res, "tickers": [mark(t) for t in res.get("tickers", [])]})

    @app.get("/static/market.js")
    def market_js():
        with open(os.path.join(STATIC, "market.js"), encoding="utf-8") as fh:
            return Response(fh.read(), media_type="text/javascript")

    @app.get("/static/chartstudies.js")
    def chartstudies_js():
        """Indicator drawing shared by the Markets and Replay charts."""
        with open(os.path.join(STATIC, "chartstudies.js"), encoding="utf-8") as fh:
            return Response(fh.read(), media_type="text/javascript")

    @app.get("/markets")
    def markets(request: Request, symbol: str = "", tf: str = "15m"):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        symbol, tf = pick(symbol, tf)
        now = int((clock or time.time)() * 1000)
        rows, bars = [], []
        try:
            db = CandleStore(settings.db_path)
            try:
                hot = set(gainers.symbols(gainers.path_for("futures" if futures_mode() else "spot")))
                mine = set(held())
                rows = [{**_ticker(db, exchange, s, now), "gainer": s in hot, "held": s in mine} for s in current()]
                if symbol:
                    bars = db.load(exchange, symbol, tf, start=now - BARS * TF_MS.get(tf, 900_000))
            finally:
                db.close()
        except Exception:
            pass  # the live script fills the page from Bybit anyway
        used = _strategy_indicators(registry, "futures" if futures_mode() else "spot")
        tabs = "".join(f'<button type="button" class="seg{" active" if t == tf else ""}" data-tf="{t}">{t}</button>'
                       for t in tfs)
        body = (
            f'<div id="market" data-symbol="{_e(symbol)}" data-tf="{_e(tf)}">'
            f'<div id="tickers" class="tickers">{_ticker_buttons(rows, symbol)}</div>'
            f'<div class="card chart-card"><div class="chart-head"><h2 id="chart-title">{_e(symbol)}</h2>'
            f'{"<span class=badge>perpetual</span>" if futures_mode() else ""}'
            f'<div class="segs js-only">{tabs}</div></div>'
            f'<div id="studies" class="studies js-only" aria-label="indicators">{_study_chips(used)}</div>'
            f'{_used_line(used)}'
            f'<div id="ohlc" class="ohlc small"></div>'
            f'<div id="legend" class="legend js-only"></div>'
            f'<canvas id="chart" class="js-only" aria-label="live price chart"></canvas>'
            f'<noscript>{_fallback(symbol, tf, bars)}</noscript></div>'
            '<p class="muted small">Prices and candles come from Bybit\'s public market data '
            '(no key needed) and refresh every 1.5 seconds while this page is open. Times are India time (IST) unless AITRADER_TZ says otherwise. '
            'Hover or touch the chart to read a candle and its indicators. Indicators use the same '
            'formulas as the strategies. Fibonacci levels and the volume profile cover the last 100 candles; '
            'candle pattern letters: E engulfing, H hammer, D doji, I inside bar. Triangles are the paper trader\'s entries (up: buy, down: '
            'short), dots its exits (green: profit, red: loss); an open trade shows its entry, stop '
            'and target lines. The chart only shows: it can\'t place orders.</p></div>'
            '<script src="/static/chartstudies.js" defer></script><script src="/static/market.js" defer></script>')
        status = '<span id="live-status" class="live-status"><span class="dot"></span> Connecting…</span>'
        return page("Markets", body, user, token, head_extra=status)


# key, label, colour, panel (drawn below the price) or overlay, on by default
STUDIES = [
    ("ema", "EMA 20/50/200", "#f5c542", False, True),
    ("sma", "SMA 20/50/200", "#fde68a", False, False),
    ("dc", "Donchian 20", "#60a5fa", False, False),
    ("bb", "Bollinger", "#94a3b8", False, False),
    ("vwap", "VWAP", "#f472b6", False, False),
    ("st", "Supertrend", "#22c55e", False, True),
    ("ich", "Ichimoku", "#38bdf8", False, False),
    ("psar", "Parabolic SAR", "#fb923c", False, False),
    ("sr", "Support/resistance", "#a3a3a3", False, False),
    ("kc", "Keltner", "#a78bfa", False, False),
    ("fib", "Fibonacci", "#eab308", False, False),
    ("vp", "Volume profile", "#64748b", False, False),
    ("pat", "Candle patterns", "#84cc16", False, False),
    ("smc", "FVG / order blocks / sweeps", "#2dd4bf", False, False),
    ("trades", "Agent's trades", "#7aa2ff", False, True),
    ("vol", "Volume", "#64748b", True, True),
    ("rsi", "RSI", "#c084fc", True, True),
    ("macd", "MACD", "#4cc3ff", True, True),
    ("adx", "ADX", "#f5c542", True, False),
    ("stoch", "Stoch RSI", "#2dd4bf", True, False),
    ("atr", "ATR", "#f97316", True, False),
    ("cci", "CCI", "#22d3ee", True, False),
    ("willr", "Williams %R", "#e879f9", True, False),
    ("fi", "Force index", "#34d399", True, False),
    ("obv", "OBV", "#a3e635", True, False),
    ("mfi", "MFI", "#fbbf24", True, False),
    ("cmf", "CMF", "#f87171", True, False),
    ("roc", "ROC 10", "#93c5fd", True, False),
]


# always shown, whatever the strategies use
ALWAYS_ON = {"trades", "vol"}


def _strategy_indicators(registry_path: str, market: str | None = None) -> dict[str, int]:
    """How many of the strategies ``market``'s engine trades use each chart indicator ({} when none
    can be read)."""
    if not registry_path:
        return {}
    try:
        from ..learn import Registry, picks
        from ..learn.keep import indicators_used

        only = bool(market) and picks.winners_only(registry_path, market)
        return indicators_used(Registry(registry_path), market, only)
    except Exception:
        return {}


def _study_chips(used: dict[str, int] | None = None, skip: set[str] | frozenset = frozenset()) -> str:
    """The indicator switches. With strategies paper trading, the ones they use start on."""
    def default(k: str, on: bool) -> bool:
        return k in used or k in ALWAYS_ON if used else on

    return "".join(f'<button type="button" class="chip" data-study="{k}" data-on="{int(default(k, on))}" '
                   f'aria-pressed="{str(default(k, on)).lower()}"><i style="background:{c}"></i>{_e(label)}'
                   f'{f" <small>{used[k]}</small>" if used and k in used else ""}</button>'
                   for k, label, c, _panel, on in STUDIES if k not in skip)


def _used_line(used: dict[str, int]) -> str:
    if not used:
        return ""
    labels = {k: label for k, label, *_ in STUDIES}
    names = ", ".join(f"{_e(labels[k])} ({n})" for k, n in used.items() if k in labels)
    return (f'<p class="small muted js-only">Indicators the strategies this engine trades use (how many use each): '
            f'{names}. <button type="button" id="studies-reset" class="linkish">Show just these</button></p>')


def _ticker(db: CandleStore, exchange: str, symbol: str, now: int) -> dict:
    """Last saved price and the 24h change, from the finest timeframe that has data."""
    for tf in ("1m", "5m", "15m", "1h"):
        recent = db.load(exchange, symbol, tf, start=now - DAY_MS - TF_MS[tf])
        if recent:
            last, base = recent[-1], recent[0].open
            return {"symbol": symbol, "price": last.close,
                    "change": (last.close / base - 1) * 100 if base else None}
    return {"symbol": symbol, "price": None, "change": None}


def _ticker_buttons(rows: list[dict], current: str) -> str:
    out = ""
    for r in rows:
        ch = r.get("change")
        cls = "" if ch is None else "good" if ch >= 0 else "bad"
        out += (f'<button type="button" class="tick{" active" if r["symbol"] == current else ""}" '
                f'data-symbol="{_e(r["symbol"])}"><span class="tick-sym">{_e(r["symbol"])}'
                f'{" <span class=hot>top gainer</span>" if r.get("gainer") else ""}'
                f'{" <span class=hot>open position</span>" if r.get("held") else ""}</span>'
                f'<span class="tick-px">{"-" if r["price"] is None else _price(r["price"])}</span>'
                f'<span class="tick-ch {cls}">{"" if ch is None else f"{ch:+.2f}%"}</span></button>')
    return out


def _price(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6f}"


def _fallback(symbol: str, tf: str, bars: list) -> str:
    links = " ".join(f'<a href="/markets?symbol={_q(symbol)}&tf={_q(t)}">{t}</a>' for t in CHART_TIMEFRAMES)
    if not bars:
        return f'<p class=muted>No saved {_e(tf)} candles yet for {_e(symbol)}. {links}</p>'
    return f'<p class=small>{links}</p>{candles_svg(bars[-BARS:])}'


def candles_svg(bars: list, w: int = 600, h: int = 340, pad: int = 6, axis: int = 92) -> str:
    """Inline SVG candlesticks (green up, red down) with the high, low and last price marked."""
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    span = (hi - lo) or hi * 0.001 or 1
    step = (w - axis - pad) / len(bars)
    body_w = max(1.0, step * 0.7)

    def y(v: float) -> float:
        return pad + (hi - v) / span * (h - 2 * pad - 22)

    parts = []
    for i, b in enumerate(bars):
        x = pad + i * step + step / 2
        color = "var(--good)" if b.close >= b.open else "var(--bad)"
        top, bot = y(max(b.open, b.close)), y(min(b.open, b.close))
        parts.append(f'<line x1="{x:.1f}" y1="{y(b.high):.1f}" x2="{x:.1f}" y2="{y(b.low):.1f}" stroke="{color}"/>'
                     f'<rect x="{x - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
                     f'height="{max(1.0, bot - top):.1f}" fill="{color}"/>')
    last = bars[-1].close
    ly, lx = y(last), w - axis
    labels = (f'<line x1="{pad}" y1="{ly:.1f}" x2="{lx}" y2="{ly:.1f}" stroke="var(--accent)" stroke-dasharray="4 4"/>'
              f'<text x="{lx + 4}" y="{ly + 4:.1f}" fill="var(--accent)" font-size="17" font-weight="600">{_price(last)}</text>'
              f'<text x="{lx + 4}" y="{y(hi) + 12:.1f}" fill="var(--muted)" font-size="15">{_price(hi)}</text>'
              f'<text x="{lx + 4}" y="{y(lo):.1f}" fill="var(--muted)" font-size="15">{_price(lo)}</text>'
              f'<text x="{pad}" y="{h - 2}" fill="var(--muted)" font-size="14">{_e(_utc(bars[0].ts))}</text>'
              f'<text x="{lx}" y="{h - 2}" fill="var(--muted)" font-size="14" text-anchor="end">'
              f'{_e(_utc(bars[-1].ts))} {_e(tz_label(bars[-1].ts / 1000))}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="price chart" class="candles">'
            f'{"".join(parts)}{labels}</svg>')


def _utc(ms: int) -> str:
    return local(ms / 1000, "%m-%d %H:%M", suffix=False)
