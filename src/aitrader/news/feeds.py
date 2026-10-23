"""Free, keyless news and sentiment sources the news service reads before the AI searches the web.

- Crypto Fear & Greed index (alternative.me).
- Coins trending in searches on CoinGecko (what people are looking up right now).
- Headlines from crypto news sites and world news (RSS): wars, sanctions, regulation, hacks.
- Every Bybit announcement (its public announcements API): new listings, delistings, maintenance,
  launchpools, rule changes. Bybit is the exchange the engines trade on, so these come first.

Every fetch fails soft: a source that can't be read is left out and named in ``errors``.
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

log = logging.getLogger(__name__)

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=2"
TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
CRYPTO_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]
WORLD_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
]
BYBIT_NEWS_URL = "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=50&page={page}"
BYBIT_NEWS_PAGES = 4  # up to 200 announcements, newest first
BYBIT_NEWS_DAYS = 3
DAY_MS = 86_400_000
MAX_BYTES = 3_000_000


def fetch_url(url: str, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": "aitrader/1.0 (news reader)",
                                               "accept": "application/json, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_BYTES)


def fear_greed(fetch=fetch_url) -> dict:
    data = json.loads(fetch(FEAR_GREED_URL))["data"]
    now, prev = data[0], (data[1] if len(data) > 1 else data[0])
    return {"value": int(now["value"]), "label": str(now["value_classification"]),
            "yesterday": int(prev["value"])}


def trending(fetch=fetch_url, limit: int = 10) -> list[dict]:
    coins = json.loads(fetch(TRENDING_URL)).get("coins", [])
    out = []
    for c in coins[:limit]:
        item = c.get("item", {})
        if item.get("symbol"):
            out.append({"symbol": str(item["symbol"]).upper()[:15], "name": str(item.get("name", ""))[:60],
                        "rank": item.get("market_cap_rank")})
    return out


def _when(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    try:
        return int(email.utils.parsedate_to_datetime(text).timestamp() * 1000)  # RSS: RFC 822
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)  # Atom
    except ValueError:
        return None


def parse_feed(raw: bytes, source: str) -> list[dict]:
    """Items of an RSS 2.0 or Atom feed: title, link, published (ms) and source."""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: c for c in el}
        title = (fields["title"].text or "").strip() if "title" in fields else ""
        link = ""
        if "link" in fields:
            link = (fields["link"].text or fields["link"].get("href") or "").strip()
        date = next((fields[k].text for k in ("pubDate", "published", "updated", "date") if k in fields), None)
        if title and link.startswith(("https://", "http://")):
            out.append({"title": " ".join(title.split())[:300], "url": link[:500],
                        "published": _when(date), "source": source})
    return out


def headlines(feeds: list[tuple[str, str]], fetch=fetch_url, now_ms: int | None = None,
              max_age_ms: int = DAY_MS, per_feed: int = 15) -> tuple[list[dict], list[str]]:
    now_ms = now_ms or int(time.time() * 1000)
    items, errors = [], []
    for name, url in feeds:
        try:
            got = [i for i in parse_feed(fetch(url), name)
                   if i["published"] is None or now_ms - i["published"] <= max_age_ms]
            items += got[:per_feed]
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    items.sort(key=lambda i: i["published"] or 0, reverse=True)
    return items, errors


def _ms(v) -> int | None:
    return int(v) if str(v or "").isdigit() else None


def bybit_announcements(fetch=fetch_url, now_ms: int | None = None, days: int = BYBIT_NEWS_DAYS) -> list[dict]:
    """Bybit's news and events, newest first, page by page: every announcement of the last ``days``
    days (news, listings, delistings, maintenance, campaigns) plus the events still running that
    were announced before. Each has title, link, published (ms), source ("Bybit: <kind>"), kind,
    tags, a short description, and for an event its window: starts and ends (ms; None otherwise,
    since Bybit gives plain news a start equal to its end)."""
    now_ms = now_ms or int(time.time() * 1000)
    out, seen = [], set()
    for page in range(1, BYBIT_NEWS_PAGES + 1):
        data = json.loads(fetch(BYBIT_NEWS_URL.format(page=page)))
        if data.get("retCode") not in (0, None):
            raise ValueError(f"Bybit said {data.get('retMsg') or data.get('retCode')}")
        rows = (data.get("result") or {}).get("list") or []
        for r in rows:
            url, title = str(r.get("url") or ""), " ".join(str(r.get("title") or "").split())
            when = _ms(r.get("publishTime") or r.get("dateTimestamp"))
            starts, ends = _ms(r.get("startDateTimestamp")), _ms(r.get("endDateTimestamp"))
            if not (starts and ends and ends > starts):
                starts = ends = None  # plain news: no event window
            running = ends is not None and ends > now_ms
            if when is not None and now_ms - when > days * DAY_MS and not running:
                continue
            if not title or not url.startswith("https://") or url in seen:
                continue
            seen.add(url)
            kind = str((r.get("type") or {}).get("title") or "Announcement")[:60]
            out.append({"title": title[:300], "url": url[:500], "published": when, "source": f"Bybit: {kind}",
                        "kind": kind, "tags": [str(t)[:40] for t in (r.get("tags") or [])][:8],
                        "description": " ".join(str(r.get("description") or "").split())[:300],
                        "starts": starts, "ends": ends})
        if len(rows) < 50:  # the last page (an event still running can be on any page, so read them all)
            break
    out.sort(key=lambda i: i["published"] or 0, reverse=True)
    return out


def gather(fetch=fetch_url, now_ms: int | None = None) -> dict:
    """Everything the free sources say right now."""
    out: dict = {"errors": []}
    for key, fn in (("fear_greed", fear_greed), ("trending", trending)):
        try:
            out[key] = fn(fetch)
        except Exception as exc:
            out[key] = None
            out["errors"].append(f"{key}: {type(exc).__name__}")
    try:
        out["bybit_announcements"] = bybit_announcements(fetch, now_ms)
    except Exception as exc:
        out["bybit_announcements"] = []
        out["errors"].append(f"Bybit announcements: {type(exc).__name__}")
    out["crypto_headlines"], err = headlines(CRYPTO_FEEDS, fetch, now_ms)
    out["errors"] += err
    out["world_headlines"], err = headlines(WORLD_FEEDS, fetch, now_ms)
    out["errors"] += err
    return out
