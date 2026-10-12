"""The Markets page: latest price and 24h change for each pair, and a candlestick chart.

Prices come from the candle database, which the ``collector`` service tops up from Binance every
minute (public data, no API key). The page reloads itself every 30 seconds.
"""

from __future__ import annotations

import time

from fastapi import Request

from ..data.store import CandleStore
from .app import _e, _q

CHART_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000}
BARS = 120
DAY_MS = 86_400_000


def add_market_routes(app, *, settings, page, guard, exchange: str = "binance", clock=None) -> None:
    def store():
        return CandleStore(settings.db_path)

    @app.get("/markets")
    def markets(request: Request, symbol: str = "", tf: str = "1h"):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        symbols = list(settings.symbols)
        symbol = symbol if symbol in symbols else (symbols[0] if symbols else "")
        tfs = [t for t in CHART_TIMEFRAMES if t in settings.timeframes] or CHART_TIMEFRAMES
        tf = tf if tf in tfs else ("1h" if "1h" in tfs else tfs[0])
        now = int((clock or time.time)() * 1000)
        try:
            db = store()
        except Exception as exc:  # no database yet: say so instead of failing
            return page("Markets", f'<p class="bad">Price database not readable ({_e(type(exc).__name__)}).</p>',
                        user, token)
        try:
            rows = [_ticker(db, exchange, s, now) for s in symbols]
            bars = db.load(exchange, symbol, tf, start=now - BARS * TF_MS.get(tf, 3_600_000)) if symbol else []
        finally:
            db.close()
        body = _table(rows, symbol, tf, now)
        body += _chart_card(symbol, tf, tfs, bars[-BARS:], now)
        body += ('<p class=muted small>Prices come from Binance (public data) and are refreshed by the '
                 'collector service every minute; this page reloads every 30 seconds.</p>')
        return page("Markets", body, user, token, refresh=30)


def _ticker(db: CandleStore, exchange: str, symbol: str, now: int) -> dict:
    """Last price, its time and the 24h change, from the finest timeframe that has data."""
    for tf in ("1m", "5m", "15m", "1h"):
        recent = db.load(exchange, symbol, tf, start=now - DAY_MS - TF_MS[tf])
        if recent:
            last = recent[-1]
            base = recent[0].open
            change = (last.close / base - 1) * 100 if base else None
            return {"symbol": symbol, "price": last.close, "ts": last.ts + TF_MS[tf], "change": change}
    return {"symbol": symbol, "price": None}


def _price(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6f}"


def _ago(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def _table(rows: list[dict], current: str, tf: str, now: int) -> str:
    if not any(r["price"] is not None for r in rows):
        return ('<p class="bad">No prices yet. The <b>collector</b> service downloads them: check it runs '
                '(<code>docker compose ps</code> on the server), or start the first-time setup on the '
                '<a href="/settings">Settings</a> page.</p>')
    out = ""
    for r in rows:
        name = _e(r["symbol"])
        link = f'<a href="/markets?symbol={_q(r["symbol"])}&tf={_q(tf)}">{name}</a>'
        if r["symbol"] == current:
            link = f"<b>{link}</b>"
        if r["price"] is None:
            out += f"<tr><td>{link}</td><td class=muted colspan=3>no data yet</td></tr>"
            continue
        ch = r["change"]
        cls = "good" if (ch or 0) >= 0 else "bad"
        age = now - r["ts"]
        stale = ' <span class="warn">(stale)</span>' if age > 15 * 60_000 else ""
        out += (f"<tr><td>{link}</td><td><b>{_price(r['price'])}</b></td>"
                f'<td class="{cls}">{"" if ch is None else f"{ch:+.2f}%"}</td>'
                f"<td class=small>{_ago(age)}{stale}</td></tr>")
    return ("<table><tr><th>Pair</th><th>Price (USDT)</th><th>24h</th>"
            f"<th>Updated</th></tr>{out}</table>")


def _chart_card(symbol: str, tf: str, tfs: list[str], bars: list, now: int) -> str:
    tabs = " ".join(
        f'<b>{t}</b>' if t == tf else f'<a href="/markets?symbol={_q(symbol)}&tf={_q(t)}">{t}</a>' for t in tfs)
    head = f'<h3>{_e(symbol)} · {_e(tf)}</h3><p class=small>{tabs}</p>'
    if not bars:
        return f'<div class="card chart">{head}<p class=muted>No {_e(tf)} candles yet for {_e(symbol)}.</p></div>'
    return f'<div class="card chart">{head}{candles_svg(bars)}</div>'


def candles_svg(bars: list, w: int = 600, h: int = 340, pad: int = 6, axis: int = 92) -> str:
    """Inline SVG candlesticks (green up, red down) with the high, low and last price marked."""
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    span = (hi - lo) or hi * 0.001 or 1
    plot_w = w - axis - pad
    step = plot_w / len(bars)
    body_w = max(1.0, step * 0.7)

    def y(v: float) -> float:
        return pad + (hi - v) / span * (h - 2 * pad - 22)

    parts = []
    for i, b in enumerate(bars):
        x = pad + i * step + step / 2
        up = b.close >= b.open
        color = "var(--good)" if up else "var(--bad)"
        top, bot = y(max(b.open, b.close)), y(min(b.open, b.close))
        parts.append(f'<line x1="{x:.1f}" y1="{y(b.high):.1f}" x2="{x:.1f}" y2="{y(b.low):.1f}" stroke="{color}"/>'
                     f'<rect x="{x - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
                     f'height="{max(1.0, bot - top):.1f}" fill="{color}"/>')
    last = bars[-1].close
    ly = y(last)
    lx = w - axis
    labels = (f'<line x1="{pad}" y1="{ly:.1f}" x2="{lx}" y2="{ly:.1f}" stroke="var(--accent)" stroke-dasharray="4 4"/>'
              f'<text x="{lx + 4}" y="{ly + 4:.1f}" fill="var(--accent)" font-size="17" font-weight="600">{_price(last)}</text>'
              f'<text x="{lx + 4}" y="{y(hi) + 12:.1f}" fill="var(--muted)" font-size="15">{_price(hi)}</text>'
              f'<text x="{lx + 4}" y="{y(lo):.1f}" fill="var(--muted)" font-size="15">{_price(lo)}</text>'
              f'<text x="{pad}" y="{h - 2}" fill="var(--muted)" font-size="14">{_e(_utc(bars[0].ts))}</text>'
              f'<text x="{lx}" y="{h - 2}" fill="var(--muted)" font-size="14" text-anchor="end">'
              f'{_e(_utc(bars[-1].ts))} UTC</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="price chart" class="candles">'
            f'{"".join(parts)}{labels}</svg>')


def _utc(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))
