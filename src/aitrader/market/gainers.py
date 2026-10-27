"""The traded pair list: two scanners, one per trading workspace (the admin's dropdown on the Pairs
page picks which list each one makes; top gainers by default).

Lists (``LISTS``), all from Bybit's public market data:

- **Top gainers** (``gainers``): the biggest risers, scored as below.
- **Top losers** (``losers``): the biggest fallers over the window (shorts on Futures, dips on Spot).
- **Most traded** (``volume``): the highest 24h turnover.
- **New listings** (``new``): listed in the last ``NEW_DAYS`` days, newest first (only where Bybit
  gives a listing date), with a lower turnover floor (``NEW_MIN_TURNOVER``).
- **Pairs I choose** (``chosen``): the admin ticks pairs from every USDT pair (spot) or USDT
  perpetual (futures) that is open for trading; each scan saves that full list (``universe``).

Top gainers in detail:

- **Futures** (``DEFAULT_PATH``): Bybit USDT linear perpetuals that are open for trading, with
  or without a spot listing: Futures AI reads only perpetual data (``data.perp``) and trades them.
- **Spot** (``SPOT_PATH``): Bybit USDT spot pairs. Spot AI trades them.

Each scanner has its own switch on the Pairs page, its own count and its own window (1h, 4h or 24h).
A pair must not be a stablecoin or a leveraged token, must have at least ``MIN_TURNOVER`` USDT of
24h turnover, and its bid/ask spread must be at most ``MAX_SPREAD_PCT``. The candidates (the
``CANDIDATES`` biggest 24h risers that pass; on a 1h or 4h window the ``CANDIDATES_SHORT`` most
traded that pass, whatever their 24h change, since a coin down on the day can be the hour's best
riser) are then scored, in the open:

    score = rise over the window (%)
            + 2 x (relative volume - 1), capped at +6   (last hour's volume / the 24h hourly average)
            - 5 x spread (%)

and ranked by score. A pair that looks like a pump (``pump_warning``: up over ``PUMP_1H_PCT`` in an
hour, or over ``PUMP_24H_PCT`` in a day) is listed with a warning but not traded.

Each list is refreshed by the collector about every hour while its switch is on (``REFRESH_MS``)
and by ``aitrader pairs gainers``. It is dropped from trading when it is older than
``MAX_AGE_HOURS`` (the collector stopped); pairs with an open paper position keep getting
candles until it closes.

Today's biggest movers are often the most volatile: big gains are often followed by sharp drops,
spreads widen and stops slip. Every trade still goes through the usual risk limits and fee filter,
and this only changes paper trading: live orders stay locked behind the go/no-go gate.
"""

from __future__ import annotations

import json
import os
import time

from .pairs import is_leveraged_token, is_stablecoin

DEFAULT_PATH = "data/pairs/gainers.json"  # the Futures scanner (its name from before the spot one)
SPOT_PATH = "data/pairs/gainers_spot.json"
MARKETS = ("spot", "futures")
WINDOWS = ("1h", "4h", "24h")
DEFAULT_WINDOW = "24h"
MAX_SPREAD_PCT = 0.2
CANDIDATES = 40
CANDIDATES_SHORT = 80  # 1h/4h window: the most traded pairs are checked, up or down on the day
PUMP_1H_PCT, PUMP_24H_PCT = 15.0, 60.0
DEFAULT_TOP = 10
MIN_TOP, MAX_TOP = 3, 30
MIN_TURNOVER = 10_000_000  # USDT of 24h perp turnover
REFRESH_MS = 3_600_000
MAX_AGE_HOURS = 6.0
DAY_MS = 86_400_000
LISTS = {"gainers": "Top gainers", "losers": "Top losers", "volume": "Most traded", "new": "New listings",
         "chosen": "Pairs I choose"}
DEFAULT_LIST = "gainers"
NEW_DAYS = 30
NEW_MIN_TURNOVER = 1_000_000
UNIVERSE_MIN_TURNOVER = 100_000  # the full list to choose from leaves out pairs nobody trades
MAX_CHOSEN = MAX_TOP


def path_for(market: str) -> str:
    return SPOT_PATH if market == "spot" else DEFAULT_PATH


def window_of(data: dict) -> str:
    w = data.get("window")
    return w if w in WINDOWS else DEFAULT_WINDOW


def list_of(data: dict) -> str:
    lst = data.get("list")
    return lst if lst in LISTS else DEFAULT_LIST


def list_name(data: dict, market: str = "futures") -> str:
    """"Futures top gainers", "Spot pairs I choose"... for messages and titles."""
    return f"{'Spot' if market == 'spot' else 'Futures'} {LISTS[list_of(data)].lower()}"


def clean_chosen(pairs) -> list[str]:
    """``BTC/USDT``-style pairs, upper case, no repeats, at most ``MAX_CHOSEN``."""
    out = []
    for p in pairs or []:
        p = str(p).strip().upper()
        if p and "/" not in p:
            p = f"{p.removesuffix('USDT')}/USDT"
        if p.endswith("/USDT") and len(p) > 5 and p not in out:
            out.append(p)
    return out[:MAX_CHOSEN]


def clamp_top(n) -> int:
    try:
        return max(MIN_TOP, min(MAX_TOP, int(n)))
    except (TypeError, ValueError):
        return DEFAULT_TOP


def load(path: str = DEFAULT_PATH) -> dict:
    """{"on", "top", "generated", "pairs": [{"symbol", "change", "turnover", "price"}]}; {} without a file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def switch(on: bool, top: int | None = None, path: str = DEFAULT_PATH, by: str = "",
           window: str | None = None, list_name: str | None = None, chosen: list[str] | None = None) -> dict:
    data = load(path)
    data.update(on=bool(on), top=clamp_top(top if top is not None else data.get("top", DEFAULT_TOP)), by=by,
                window=window if window in WINDOWS else window_of(data),
                list=list_name if list_name in LISTS else list_of(data))
    if chosen is not None:
        data["chosen"] = clean_chosen(chosen)
    save(data, path)
    return data


def symbols(path: str = DEFAULT_PATH, now_ms: int | None = None, max_age_hours: float = MAX_AGE_HOURS) -> list[str]:
    """The pairs to trade now from the list the admin picked: [] when the switch is off or a scanned
    list is missing or stale. The admin's own pairs (``chosen``) need no scan."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    if data.get("on") and list_of(data) == "chosen":
        return clean_chosen(data.get("chosen"))
    if not data.get("on") or not data.get("generated") or now_ms - int(data["generated"]) > max_age_hours * 3_600_000:
        return []
    return [p["symbol"] for p in data.get("pairs", []) if not p.get("pump")][: clamp_top(data.get("top", DEFAULT_TOP))]


def due(path: str = DEFAULT_PATH, now_ms: int | None = None) -> bool:
    """Whether the collector should refresh the list (switch on and the list over an hour old)."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    return bool(data.get("on")) and now_ms - int(data.get("generated") or 0) >= REFRESH_MS


def _spread_pct(t: dict) -> float | None:
    bid, ask = t.get("bid"), t.get("ask")
    if not bid or not ask or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def pick(markets: dict, tickers: dict, top: int = DEFAULT_TOP, min_turnover: float = MIN_TURNOVER,
         market: str = "futures", max_spread: float = MAX_SPREAD_PCT, window: str = "24h",
         list_name: str = DEFAULT_LIST, now_ms: int | None = None) -> list[dict]:
    """The candidates for one list, before scoring. Top gainers: the ``top`` biggest 24h risers that
    pass the filters; on a 1h or 4h ``window`` the ``top`` most traded that pass, up or down on the
    day (their 1h/4h move is read from candles afterwards). Top losers the same, falling. Most
    traded: by 24h turnover. New listings: listed in the last ``NEW_DAYS`` days, newest first.
    ``markets`` and ``tickers`` are ccxt's ``load_markets`` / ``fetch_tickers`` results for that market."""
    from .pairs import _listed_ms

    now_ms = now_ms or int(time.time() * 1000)
    if list_name == "new":
        min_turnover = min(min_turnover, NEW_MIN_TURNOVER)
    rows = []
    for sym, m in markets.items():
        if market == "spot":
            if not (m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)):
                continue
        elif not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
                  and m.get("active", True)):
            continue
        base = m.get("base") or ""
        pair = f"{base}/USDT"
        if is_stablecoin(base) or is_leveraged_token(base):
            continue
        t = tickers.get(sym) or {}
        change, turnover = t.get("percentage"), t.get("quoteVolume")
        if change is None or not turnover or turnover < min_turnover:
            continue
        spread = _spread_pct(t)
        if spread is not None and spread > max_spread:
            continue
        row = {"symbol": pair, "contract": sym, "change": round(float(change), 2), "turnover": float(turnover),
               "price": t.get("last"), "spread": None if spread is None else round(spread, 4)}
        if list_name == "new":
            listed = _listed_ms(m)
            if not listed or now_ms - listed > NEW_DAYS * DAY_MS:
                continue
            row["listed"] = listed
        rows.append(row)
    n = max(1, int(top))
    if list_name == "new":
        return sorted(rows, key=lambda r: r["listed"], reverse=True)[:n]
    if list_name == "volume" or window in ("1h", "4h"):
        rows.sort(key=lambda r: r["turnover"], reverse=True)
        return rows[:n]
    if list_name == "losers":
        rows.sort(key=lambda r: r["change"])
        return [r for r in rows if r["change"] < 0][:n]
    rows.sort(key=lambda r: r["change"], reverse=True)
    return [r for r in rows if r["change"] > 0][:n]


def universe(markets: dict, tickers: dict, market: str = "futures") -> list[dict]:
    """Every pair the admin can choose from: open for trading, quoted in USDT, not a stablecoin or a
    leveraged token, with some 24h turnover; the most traded first."""
    rows = pick(markets, tickers, top=10_000, min_turnover=UNIVERSE_MIN_TURNOVER, market=market,
                max_spread=float("inf"), list_name="volume")
    return [{"symbol": r["symbol"], "turnover": r["turnover"], "change": r["change"]} for r in rows]


def pump_warning(row: dict) -> str:
    """Why a pair looks like a pump (listed, not traded), or ""."""
    if (row.get("change_1h") or 0) > PUMP_1H_PCT:
        return f"up {row['change_1h']:.0f}% in an hour"
    if (row.get("change") or 0) > PUMP_24H_PCT:
        return f"up {row['change']:.0f}% in a day"
    return ""


def score(row: dict, window: str = DEFAULT_WINDOW, list_name: str = DEFAULT_LIST) -> float:
    """The open score. Top gainers: the rise over the window, plus a capped bonus for unusual volume,
    minus the spread. Top losers: the fall instead of the rise. Most traded, new listings and the
    admin's own pairs: the 24h turnover in millions of USDT."""
    if list_name in ("volume", "new", "chosen"):
        return round((row.get("turnover") or 0.0) / 1e6, 1)
    move = row.get({"1h": "change_1h", "4h": "change_4h"}.get(window, "change")) or 0.0
    relvol = row.get("relvol")
    bonus = 0.0 if relvol is None else max(-2.0, min(6.0, 2.0 * (relvol - 1.0)))
    return round((-move if list_name == "losers" else move) + bonus - 5.0 * (row.get("spread") or 0.0), 2)


def enrich(row: dict, ohlcv: list) -> dict:
    """Add the 1h and 4h rise and the relative volume from hourly candles ([ts, o, h, l, c, v], oldest
    first, the newest possibly still forming)."""
    closes = [float(c[4]) for c in ohlcv if c and c[4]]
    vols = [float(c[5] or 0) for c in ohlcv if c and c[4]]
    out = dict(row)
    if len(closes) >= 2:
        out["change_1h"] = round((closes[-1] / closes[-2] - 1) * 100, 2)
    if len(closes) >= 5:
        out["change_4h"] = round((closes[-1] / closes[-5] - 1) * 100, 2)
    past = vols[-25:-1]
    if past and sum(past) > 0:
        out["relvol"] = round(vols[-1] / (sum(past) / len(past)), 2)
    return out


def rank(rows: list[dict], window: str = DEFAULT_WINDOW, list_name: str = DEFAULT_LIST) -> list[dict]:
    """Score, flag pumps, and order: the best first. Top gainers keep rises over the window only,
    top losers falls only; new listings stay newest first; the admin's own pairs stay in their order."""
    out = []
    for r in rows:
        # a fall is never a pump; the admin's own pairs trade as chosen
        pump = "" if list_name in ("losers", "chosen") else pump_warning(r)
        r = {**r, "score": score(r, window, list_name), "pump": pump}
        move = r.get({"1h": "change_1h", "4h": "change_4h"}.get(window, "change"))
        if list_name == "gainers" and not (move is not None and move > 0):
            continue
        if list_name == "losers" and not (move is not None and move < 0):
            continue
        out.append(r)
    if list_name in ("gainers", "losers", "volume"):
        out.sort(key=lambda r: r["score"], reverse=True)
    return out


def spot_client(exchange_id: str = "bybit"):
    """A ccxt async client for public spot data."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def perp_client(exchange_id: str = "bybit"):
    """A ccxt async client for public data whose tickers are the USDT linear perpetuals."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True,
                                             "options": {"defaultType": "swap", "defaultSubType": "linear"}})


def client_for(market: str, exchange_id: str = "bybit"):
    return spot_client(exchange_id) if market == "spot" else perp_client(exchange_id)


async def refresh(client, path: str = DEFAULT_PATH, top: int | None = None, now_ms: int | None = None,
                  market: str | None = None, window: str | None = None, limits_path: str | None = None) -> dict:
    """Scan, score and save one market's gainers (the switch stays as it is). On Futures with
    ``limits_path``, each coin's maximum leverage is saved too, so a new gainer is never traded
    above Bybit's limit for it."""
    import logging

    market = market or ("spot" if os.path.abspath(path) == os.path.abspath(SPOT_PATH) else "futures")
    data = load(path)
    top = clamp_top(top if top is not None else data.get("top", DEFAULT_TOP))
    window = window if window in WINDOWS else window_of(data)
    lst = list_of(data)
    now_ms = now_ms or int(time.time() * 1000)
    markets = await client.load_markets()
    if market == "futures" and limits_path:
        from . import perp_limits

        perp_limits.save(perp_limits.from_markets(markets), limits_path, now_ms)
    tickers = await client.fetch_tickers()
    everything = universe(markets, tickers, market)
    if lst == "chosen":  # the admin's own pairs: their numbers, in the admin's order
        chosen = clean_chosen(data.get("chosen"))
        rows = pick(markets, tickers, top=10_000, min_turnover=0, market=market, max_spread=float("inf"),
                    list_name="volume")
        by_pair = {r["symbol"]: r for r in rows}
        cands = [by_pair[p] for p in chosen if p in by_pair]
    else:
        short = window in ("1h", "4h")
        cands = pick(markets, tickers, top=CANDIDATES_SHORT if short and lst in ("gainers", "losers") else CANDIDATES,
                     market=market, window=window, list_name=lst, now_ms=now_ms)
    rows = []
    for r in cands:
        try:
            r = enrich(r, await client.fetch_ohlcv(r["contract"], "1h", limit=26))
        except Exception as exc:  # one coin's candles failing must not stop the scan
            logging.getLogger(__name__).warning("pair list: no candles for %s: %s", r["symbol"], exc)
        rows.append(r)
    data.update(top=top, window=window, market=market, generated=now_ms, list=lst,
                scanned=len(rows), pairs=rank(rows, window, lst)[:MAX_TOP],  # ``top`` decides what trades
                universe=[u["symbol"] for u in everything])
    save(data, path)
    return data
