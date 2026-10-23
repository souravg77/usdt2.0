"""X (Twitter): watch chosen accounts and search what people post about each pair.

Read-only, with the app's Bearer token (``X_BEARER_TOKEN``, saved on the Settings page). The
agent never posts, likes or follows from your account; "following" someone here means adding
them to the watch list, which the agent reads.

- Watch list: Elon Musk and Donald Trump to start, plus anyone you add on the News page. Only
  posts newer than the last one seen are fetched (``since_id``), so each post is paid once.
- Pair search: the most relevant recent posts about each pair (no retweets), five pairs per
  brief, taking turns.
- Suggestions: the authors of those posts with the most followers and engagement, as people you
  might add to the watch list.

X charges per post read ($0.005 each, Oct 2026). ``AITRADER_X_DAILY_READS`` caps posts read per
UTC day (default 800, about 4 USD); when it is reached the reader stops until the next day.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.x.com/2"
DEFAULT_WATCH = ["elonmusk", "realDonaldTrump"]
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
POST_FIELDS = "created_at,public_metrics,author_id"
USER_FIELDS = "username,name,public_metrics,verified"
KEEP_POSTS = 150
COIN_WORDS = {  # extra words people use for a coin, besides its ticker
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "BNB": ["binance coin"],
    "XRP": ["ripple"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "TRX": ["tron"],
}


class XError(RuntimeError):
    pass


def coin_terms(symbol: str) -> list[str]:
    base = re.sub(r"^1000+(?=[A-Z])", "", symbol.split("/")[0].upper())  # Bybit's 1000PEPE is PEPE
    return [base, *COIN_WORDS.get(base, [])]


def mentions(text: str, symbols: list[str]) -> list[str]:
    """Pairs a text talks about: $TICKER, #TICKER, the ticker as a word, Bybit's TICKERUSDT, or the
    coin's name. A one-letter ticker counts only as $X, #X, X/USDT or XUSDT, or every "Q3" or "M&A"
    would match."""
    low = text.lower()
    out = []
    for s in symbols:
        terms = coin_terms(s)
        tick = re.escape(terms[0])
        word = (rf"(?<![A-Za-z0-9])[$#]{tick}(?![A-Za-z0-9])|\b{tick}/?USDT\b" if len(terms[0]) < 2
                else rf"(?<![A-Za-z0-9])[$#]?{tick}(?:USDT)?(?![A-Za-z0-9])")
        if re.search(word, text) or any(re.search(rf"\b{re.escape(w)}\b", low) for w in terms[1:]):
            out.append(s)
    return out


def http_get(url: str, token: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {token}", "user-agent": "aitrader"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail") or ""
        except Exception:
            detail = ""
        raise XError(f"X API HTTP {exc.code}{': ' + str(detail)[:160] if detail else ''}") from None


class XClient:
    def __init__(self, token: str | None = None, get=http_get):
        self.token = token or os.environ.get("X_BEARER_TOKEN", "")
        if not self.token:
            raise XError("X_BEARER_TOKEN is not set; add it on the dashboard's Settings page")
        self._get = get

    def get(self, path: str, **params) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._get(f"{API}{path}?{q}", self.token)

    def users(self, usernames: list[str]) -> list[dict]:
        if not usernames:
            return []
        return self.get("/users/by", usernames=",".join(usernames), **{"user.fields": USER_FIELDS}).get("data", [])

    def timeline(self, user_id: str, since_id: str | None = None, max_results: int = 10) -> list[dict]:
        data = self.get(f"/users/{user_id}/tweets", since_id=since_id, max_results=max_results,
                        exclude="retweets,replies", **{"tweet.fields": POST_FIELDS})
        return data.get("data", [])

    def search(self, query: str, max_results: int = 10) -> tuple[list[dict], dict]:
        data = self.get("/tweets/search/recent", query=query, max_results=max_results, sort_order="relevancy",
                        expansions="author_id", **{"tweet.fields": POST_FIELDS, "user.fields": USER_FIELDS})
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        return data.get("data", []), users


def _post(p: dict, user: dict | None, symbols: list[str]) -> dict:
    m = p.get("public_metrics") or {}
    username = (user or {}).get("username", "")
    return {"id": p["id"], "username": username, "name": (user or {}).get("name", ""),
            "text": " ".join(str(p.get("text", "")).split())[:600], "created_at": p.get("created_at", ""),
            "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0),
            "url": f"https://x.com/{username or 'i'}/status/{p['id']}",
            "pairs": mentions(str(p.get("text", "")), symbols)}


class XReader:
    """Keeps ``data/news/x.json``: the watch list, the newest posts, pair searches, suggestions."""

    def __init__(self, path: str = "data/news/x.json", client: XClient | None = None,
                 daily_reads: int | None = None, clock=time.time):
        self.path, self._client, self.clock = path, client, clock
        self.daily_reads = daily_reads or int(os.environ.get("AITRADER_X_DAILY_READS", "800") or 800)
        self.state = self._load()

    @property
    def client(self) -> XClient:
        if self._client is None:
            self._client = XClient()
        return self._client

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        state.setdefault("watch", [{"username": u} for u in DEFAULT_WATCH])
        for key, empty in (("since", {}), ("posts", []), ("pair_posts", {}), ("suggested", []),
                           ("usage", {}), ("errors", [])):
            state.setdefault(key, empty)
        return state

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1)
        os.replace(tmp, self.path)

    # ---- watch list (edited on the News page) --------------------------------------------------

    def add(self, username: str) -> str:
        username = username.strip().lstrip("@")
        if not HANDLE.match(username):
            raise ValueError("that isn't an X username (letters, digits and _, up to 15)")
        if any(w["username"].lower() == username.lower() for w in self.state["watch"]):
            return username
        self.state["watch"].append({"username": username})
        self.state["suggested"] = [s for s in self.state["suggested"] if s["username"].lower() != username.lower()]
        self.save()
        return username

    def remove(self, username: str) -> None:
        self.state["watch"] = [w for w in self.state["watch"] if w["username"].lower() != username.lower()]
        self.save()

    # ---- spending cap ------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def reads_today(self) -> int:
        u = self.state["usage"]
        return u.get("reads", 0) if u.get("day") == self._today() else 0

    def _spend(self, n: int) -> None:
        self.state["usage"] = {"day": self._today(), "reads": self.reads_today() + n}

    def _room(self, want: int) -> bool:
        return self.reads_today() + want <= self.daily_reads

    # ---- reading -----------------------------------------------------------------------------

    def check_watch(self, symbols: list[str]) -> list[dict]:
        """New posts from watched accounts; returns them (newest first)."""
        errors, new = [], []
        missing = [w["username"] for w in self.state["watch"] if not w.get("id")]
        if missing and self._room(len(missing)):
            try:
                found = {u["username"].lower(): u for u in self.client.users(missing)}
                self._spend(len(found))
                for w in self.state["watch"]:
                    u = found.get(w["username"].lower())
                    if u:
                        w.update(id=u["id"], username=u["username"], name=u.get("name", ""),
                                 followers=(u.get("public_metrics") or {}).get("followers_count"))
            except XError as exc:
                errors.append(str(exc))
        for w in self.state["watch"]:
            if not w.get("id"):
                continue
            if not self._room(10):
                errors.append(f"daily X read cap of {self.daily_reads} posts reached")
                break
            since = self.state["since"].get(w["id"])
            try:
                posts = self.client.timeline(w["id"], since_id=since, max_results=10 if since else 5)
            except XError as exc:
                errors.append(f"@{w['username']}: {exc}")
                continue
            self._spend(len(posts))
            if posts:
                self.state["since"][w["id"]] = max((p["id"] for p in posts), key=int)
                new += [{**_post(p, w, symbols), "watched": True} for p in posts]
        new.sort(key=lambda p: int(p["id"]), reverse=True)
        self.state["posts"] = (new + self.state["posts"])[:KEEP_POSTS]
        self.state["errors"] = errors
        self.state["checked"] = int(self.clock() * 1000)
        self.save()
        return new

    def search_pairs(self, symbols: list[str], per_pair: int = 10, max_pairs: int = 5) -> dict:
        """Search up to ``max_pairs`` pairs per call, taking turns, so 20 pairs fit the daily cap."""
        errors = list(self.state.get("errors", []))
        authors: dict[str, dict] = {}
        start = self.state.get("rotate", 0) % max(len(symbols), 1)
        turn = (symbols[start:] + symbols[:start])[:max_pairs]
        self.state["rotate"] = start + len(turn)
        for s in turn:
            if not self._room(per_pair):
                errors.append(f"daily X read cap of {self.daily_reads} posts reached")
                break
            terms = coin_terms(s)
            query = "(" + " OR ".join([f"${terms[0]}", f"#{terms[0]}", *terms[1:]]) + ") -is:retweet -is:reply lang:en"
            try:
                posts, users = self.client.search(query, per_pair)
            except XError as exc:
                errors.append(f"search {s}: {exc}")
                continue
            self._spend(len(posts))
            self.state["pair_posts"][s] = [_post(p, users.get(p.get("author_id")), symbols) for p in posts]
            for p in posts:
                u = users.get(p.get("author_id"))
                if not u:
                    continue
                a = authors.setdefault(u["username"], {"username": u["username"], "name": u.get("name", ""),
                                                       "followers": (u.get("public_metrics") or {}).get("followers_count", 0),
                                                       "posts": 0, "likes": 0, "pairs": []})
                m = p.get("public_metrics") or {}
                a["posts"] += 1
                a["likes"] += m.get("like_count", 0) + 2 * m.get("retweet_count", 0)
                if s not in a["pairs"]:
                    a["pairs"].append(s)
        self._suggest(authors)
        self.state["errors"] = errors
        self.state["searched"] = int(self.clock() * 1000)
        self.save()
        return self.state["pair_posts"]

    def _suggest(self, authors: dict) -> None:
        """Merge new authors into the suggestions, best first (reach times engagement)."""
        watched = {w["username"].lower() for w in self.state["watch"]}
        merged = {s["username"]: s for s in self.state["suggested"]}
        for a in authors.values():
            if a["username"].lower() in watched or (a["followers"] or 0) < 10_000:
                continue
            old = merged.get(a["username"])
            merged[a["username"]] = {**a, "seen": (old or {}).get("seen", 0) + 1,
                                     "likes": max(a["likes"], (old or {}).get("likes", 0))}
        ranked = sorted(merged.values(), reverse=True,
                        key=lambda s: (s["followers"] or 0) ** 0.5 * (1 + s["likes"]) ** 0.5 * s["seen"])
        self.state["suggested"] = ranked[:15]

    def for_brief(self, max_posts: int = 30) -> dict:
        """What the AI news analyst gets: recent watched posts and the top posts per pair."""
        return {"watched": self.state["posts"][:max_posts],
                "pairs": {s: ps[:5] for s, ps in self.state["pair_posts"].items()}}
