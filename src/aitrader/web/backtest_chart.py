"""The Backtest result's chart: each strategy's running total in R over the period (and all of
them together), drawn as SVG on the server. The page draws it with the dashboard's colours; the
download is a standalone SVG file with fixed colours on white."""

from __future__ import annotations

import html
import time

from ..learn.select import curve

PAGE_COLORS = ("var(--accent)", "#4ea1ff", "#c084fc", "#2dd4bf", "#f472b6", "#fb923c", "#a3e635", "#94a3b8")
FILE_COLORS = ("#d97706", "#2563eb", "#7c3aed", "#0d9488", "#db2777", "#ea580c", "#65a30d", "#64748b")
MAX_LINES = 8


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def series(report: dict) -> list[tuple[str, list[tuple[int, float]]]]:
    """(name, points) for the strategies with trades, best total first (up to MAX_LINES), then
    "All together" when there are several."""
    rows = [r for r in report.get("strategies", []) if r.get("trade_list")]
    rows.sort(key=lambda r: -(r.get("total_r") or 0))
    out = [(r["id"], curve(r["trade_list"])) for r in rows[:MAX_LINES]]
    if len(rows) > 1:
        out.append(("All together", curve([t for r in rows for t in r["trade_list"]])))
    return out


def svg(report: dict, standalone: bool = False) -> str:
    """The chart, or "" when no strategy made a trade."""
    lines = series(report)
    if not lines:
        return ""
    w, h, pad, left = 960, 320, 30, 56 if standalone else 76
    legend_h = (20 if standalone else 28) * len(lines) + 10
    x0, x1 = report["start"], report["end"]
    ys = [y for _n, pts in lines for _x, y in pts] + [0.0]
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y1 = y0 + 1
    colors = FILE_COLORS if standalone else PAGE_COLORS
    fs = 12 if standalone else 18  # the page shrinks the chart on a phone: larger letters stay readable
    ink, muted, grid = ("#111827", "#6b7280", "#d1d5db") if standalone else ("var(--text)", "var(--muted)", "var(--line)")

    def X(x):
        return left + (min(max(x, x0), x1) - x0) / max(1, x1 - x0) * (w - left - pad)

    def Y(y):
        return h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)

    parts = [f'<line x1="{left}" y1="{Y(0):.1f}" x2="{w - pad}" y2="{Y(0):.1f}" stroke="{grid}" stroke-dasharray="4 4"/>',
             f'<text x="{left - 6}" y="{Y(0) + 4:.1f}" fill="{muted}" font-size="{fs}" text-anchor="end">0R</text>',
             f'<text x="{left - 6}" y="{pad + 4}" fill="{muted}" font-size="{fs}" text-anchor="end">{y1:+.1f}R</text>',
             f'<text x="{left - 6}" y="{h - pad + 4}" fill="{muted}" font-size="{fs}" text-anchor="end">{y0:+.1f}R</text>',
             f'<text x="{left}" y="{h - 8}" fill="{muted}" font-size="{fs}">{_day(x0)}</text>',
             f'<text x="{w - pad}" y="{h - 8}" fill="{muted}" font-size="{fs}" text-anchor="end">{_day(x1 - 1)}</text>']
    for i, (name, pts) in enumerate(lines):
        together = name == "All together"
        color = ink if together else colors[i % len(colors)]
        d = [f"M{X(x0):.1f},{Y(0):.1f}"]  # a step line: the total only moves when a trade closes
        for x, y in pts:
            d.append(f"H{X(x):.1f}V{Y(y):.1f}")
        d.append(f"H{X(x1):.1f}")
        parts.append(f'<path d="{"".join(d)}" fill="none" stroke="{color}" stroke-width="{3 if together else 2}"/>')
        ly = h + 18 + (20 if standalone else 28) * i
        parts.append(f'<line x1="{left}" y1="{ly - 4}" x2="{left + 22}" y2="{ly - 4}" stroke="{color}" stroke-width="3"/>'
                     f'<text x="{left + 30}" y="{ly}" fill="{ink}" font-size="{fs + 2}">{html.escape(name)}: '
                     f'{pts[-1][1]:+.1f}R over {len(pts)} trades</text>')
    title = html.escape(f"{report.get('engine', '')} backtest {_day(x0)} to {_day(x1 - 1)}: running total in R")
    bg = '<rect width="100%" height="100%" fill="#ffffff"/>' if standalone else ""
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h + legend_h}" '
            f'width="{w}" height="{h + legend_h}" role="img" aria-label="{title}">' if standalone else
            f'<svg viewBox="0 0 {w} {h + legend_h}" role="img" aria-label="{title}" style="width:100%;height:auto">')
    return head + bg + f"<title>{title}</title>" + "".join(parts) + "</svg>"
