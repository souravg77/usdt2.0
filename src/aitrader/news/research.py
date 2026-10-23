"""Trader-research log: what traders and researchers publicly report about our strategy families.

Turned off: the search needed the Anthropic API and its web search, which this platform no longer
uses, and the local model (``aitrader.llm``) can't search the web. The findings already saved stay
readable on the Research page. What follows describes how the log was filled.

Claude searched the web (Reddit, TradingView, blogs, exchange and quant research, papers) for
public reports on the strategy families this platform runs: which indicators work or fail, false
signals in ranging and trending markets, backtest-versus-live gaps, slippage, fees, API and bot
problems, overfitting. Each finding is saved with its URL, source, the date found, the source's
own date when known, the strategy family, the claim in one sentence, the kind of evidence
(anecdote / backtest shown / live record shown / peer-reviewed / other) and a credibility note.

Findings are research inputs only. Nothing here changes trading, risk limits or a strategy's
status: no trading, risk or strategy code reads this log, and a finding can at most prompt a
person to write a new candidate strategy, which then has to pass the same backtest, out-of-sample
and paper gates as any other. Votes, testimonials and win-rate screenshots are not evidence of
profit.

Fetched web content is untrusted: the prompt tells Claude to treat it as data, the answer is
checked against fixed fields and values here, and the dashboard escapes every field and links
only http(s) URLs. The log is ``data/news/research.jsonl``, one finding per line, unique by URL.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .service import DEFAULT_DIR

FAMILIES = {
    "trend-pullback": "buy a pullback inside an uptrend (moving-average or trendline support)",
    "breakout": "buy a break of a range, flat top, flag or new high, often with volume",
    "buy-the-dip": "buy oversold dips or mean reversion back to an average",
    "volatility-squeeze": "Bollinger/Keltner squeeze, then a volatility expansion",
    "divergence": "oscillator divergence (RSI, CCI, MACD) at a low",
    "ma-crossover": "moving-average crossovers",
    "price-action": "candle patterns: inside bars, three-bar reversals, doji, Heikin-Ashi",
    "backtesting-execution": "overfitting, backtest-vs-live gaps, fees, slippage, API and bot reliability",
}
EVIDENCE = ("anecdote", "backtest shown", "live record shown", "peer-reviewed", "other")
MAX_FINDINGS = 15

def log_path(news_dir: str = DEFAULT_DIR) -> str:
    return os.path.join(news_dir, "research.jsonl")


def url_key(url: str) -> str:
    """The URL used to spot duplicates: lower-case scheme and host, no fragment, no trailing slash."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def load(news_dir: str = DEFAULT_DIR) -> list[dict]:
    """Every saved finding, newest first. Unreadable lines are skipped."""
    out = []
    try:
        with open(log_path(news_dir), encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("url"):
                    out.append(row)
    except OSError:
        return []
    return out[::-1]


def add(findings: list[dict], news_dir: str = DEFAULT_DIR) -> list[dict]:
    """Append the findings whose URL is not in the log yet; returns the ones added."""
    seen = {url_key(f["url"]) for f in load(news_dir)}
    new = []
    for f in findings:
        k = url_key(f["url"])
        if k not in seen:
            seen.add(k)
            new.append(f)
    if new:
        os.makedirs(news_dir, exist_ok=True)
        with open(log_path(news_dir), "a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(f) + "\n" for f in new)
    return new


def _url(u) -> str:
    u = str(u or "").strip()
    return u[:500] if u.lower().startswith(("https://", "http://")) and not re.search(r"\s", u) else ""


def _text(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _date(v) -> str:
    v = str(v or "").strip()[:10]
    return v if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v) else ""


def clean(raw: dict, now_ms: int) -> list[dict]:
    """Keep findings with an http(s) URL, a known family and a claim; unknown evidence is 'other'."""
    found = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    out, urls = [], set()
    for f in (raw.get("findings") or [])[:MAX_FINDINGS] if isinstance(raw, dict) else []:
        if not isinstance(f, dict):
            continue
        url, family, claim = _url(f.get("url")), str(f.get("family") or "").strip().lower(), _text(f.get("claim"), 400)
        if not url or family not in FAMILIES or not claim or url_key(url) in urls:
            continue
        urls.add(url_key(url))
        evidence = str(f.get("evidence") or "").strip().lower()
        out.append({"url": url, "source": _text(f.get("source"), 160) or urlsplit(url).netloc,
                    "date_found": found, "source_date": _date(f.get("source_date")), "family": family,
                    "claim": claim, "evidence": evidence if evidence in EVIDENCE else "other",
                    "credibility": _text(f.get("credibility"), 600)})
    return out


OFF = ("trader research is turned off: it searched the web through the Anthropic API, which this "
       "platform no longer uses, and the local model can't search the web")


def update(news_dir: str = DEFAULT_DIR, events=None, **_ignored) -> dict:
    """Turned off (see the module notes): saves nothing and says why."""
    if events is not None:
        events.emit("research", OFF, level="warning")
    return {"added": [], "found": 0, "searches": 0, "error": OFF}
