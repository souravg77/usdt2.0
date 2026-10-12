"""News sites the AI analyst checks first when it searches the web (editable on the News page).

Stored in ``data/news/sites.json`` as plain addresses without ``https://``, e.g.
``www.cnbc.com/cryptoworld``. The AI still searches the rest of the web; these come first.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

DEFAULT_SITES = [
    "coinmarketcap.com",
    "www.cnbc.com/cryptoworld",
    "crypto.economictimes.indiatimes.com",
    "economictimes.indiatimes.com/markets/cryptocurrency",
    "www.coindesk.com",
]
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
MAX_SITES = 20


def _path(news_dir: str) -> str:
    return os.path.join(news_dir, "sites.json")


def load_sites(news_dir: str) -> list[str]:
    try:
        with open(_path(news_dir), encoding="utf-8") as fh:
            sites = json.load(fh)
        return [s for s in sites if isinstance(s, str)][:MAX_SITES]
    except (OSError, ValueError):
        return list(DEFAULT_SITES)


def _save(news_dir: str, sites: list[str]) -> None:
    os.makedirs(news_dir, exist_ok=True)
    tmp = _path(news_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sites, fh, indent=1)
    os.replace(tmp, _path(news_dir))


def normalize(text: str) -> str:
    text = text.strip()
    u = urlparse(text if "://" in text else "https://" + text)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not HOST.match(host):
        raise ValueError("that isn't a web address, e.g. www.coindesk.com")
    host = host.replace("m.economictimes.com", "economictimes.indiatimes.com")  # mobile copy of the same site
    path = re.sub(r"[^A-Za-z0-9/_.-]", "", u.path).rstrip("/")
    return (host + path)[:120]


def add_site(news_dir: str, text: str) -> str:
    site = normalize(text)
    sites = load_sites(news_dir)
    if site not in sites:
        if len(sites) >= MAX_SITES:
            raise ValueError(f"at most {MAX_SITES} sites; remove one first")
        sites.append(site)
        _save(news_dir, sites)
    return site


def remove_site(news_dir: str, site: str) -> None:
    _save(news_dir, [s for s in load_sites(news_dir) if s != site])
