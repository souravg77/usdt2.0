"""Pair ranking (agent I): which Bybit spot USDT pairs are worth trading, from public data only.

``rank_pairs`` reads every market (``load_markets``) and its 24h ticker (``fetch_tickers``), drops
what is not tradable for this agent, then checks the order book of the best candidates:

- dropped: not spot, not quoted in USDT, inactive, stablecoin against stablecoin, leveraged tokens
  (``BTC3L``, ``ETH3S``), listed under ``min_listing_days`` ago (when the
  exchange says when), and anything under the liquidity floors (24h turnover, spread, depth)
- scored (0-100): turnover 35, spread 20, depth near mid 20, volatility 10, the platform's own
  validated results on the pair 15. Volatility scores best in a middle band, so a pair never
  ranks high only because its price is moving fast.

The result is saved with its timestamp and the reasons for every pair. ``configured_symbols``
lets the collector, downloader and paper trader use the ranked list when ``AITRADER_PAIRS=ranked``
and falls back to the fixed list when the ranking is missing, empty or older than
``AITRADER_PAIRS_MAX_AGE_HOURS``.

Survivorship bias: ranking today's pairs and backtesting them over past years leaves out the
coins that were delisted or faded, which flatters backtest results. See docs/regime-and-pairs.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/pairs/ranking.json"
STABLECOINS = frozenset({
    "USDT", "USDC", "USDE", "DAI", "FDUSD", "TUSD", "PYUSD", "USDD", "BUSD", "USDP", "GUSD", "LUSD",
    "FRAX", "USD1", "RLUSD", "USDQ", "USDR", "USDJ", "SUSD", "CUSD", "EURC", "EURT", "EURI", "AEUR",
    "USDS", "USD0", "USDX",
})
_LEVERAGED = re.compile(r"^[A-Z]+\d+[LS]$")  # BTC3L, ETH3S, ADA2L
DAY_MS = 86_400_000


@dataclass(frozen=True)
class RankConfig:
    quote: str = "USDT"
    min_turnover_usdt: float = 2_000_000.0  # 24h quote volume
    max_spread_bps: float = 30.0
    min_depth_usdt: float = 25_000.0  # smaller side of the book within ``depth_pct`` of mid
    depth_pct: float = 1.0
    depth_top: int = 40  # order books fetched for this many best candidates (0 = none)
    min_listing_days: int = 30  # only applied when the exchange gives a listing date
    vol_band: tuple[float, float] = (2.0, 12.0)  # 24h high-low range, % of price, that scores best


def is_stablecoin(base: str) -> bool:
    return base.upper() in STABLECOINS


def is_leveraged_token(base: str) -> bool:
    return bool(_LEVERAGED.match(base.upper()))


async def rank_pairs(client, cfg: RankConfig | None = None, registry_path: str | None = None,
                     paper_state: str | None = None, now_ms: int | None = None,
                     exchange: str = "bybit") -> dict:
    """``client`` is a ccxt async client (public endpoints only; tests pass a fake)."""
    cfg = cfg or RankConfig()
    now_ms = now_ms or int(time.time() * 1000)
    markets = await client.load_markets()
    tickers = await client.fetch_tickers()
    validated = validated_results(registry_path, paper_state)
    excluded, candidates = [], []
    for sym, m in sorted(markets.items()):
        why = _ineligible(m, cfg, now_ms)
        if why is None:
            t = tickers.get(sym) or {}
            row = _measure(sym, m, t, now_ms)
            why = _below_floor(row, cfg)
            if why is None:
                candidates.append(row)
        if why is not None and m.get("spot") and m.get("quote") == cfg.quote:
            excluded.append({"symbol": sym, "why": why})
    for row in candidates:
        row["validated"] = validated.get(row["symbol"])
        row["score"] = _score(row, cfg)
    candidates.sort(key=lambda r: r["score"], reverse=True)
    ranked = []
    for i, row in enumerate(candidates):
        if i < cfg.depth_top:
            await _add_depth(client, row, cfg)
            why = _below_floor(row, cfg)
            if why is not None:
                excluded.append({"symbol": row["symbol"], "why": why})
                continue
        row["score"] = _score(row, cfg)
        row["reasons"] = _reasons(row, cfg)
        ranked.append(row)
    ranked.sort(key=lambda r: r["score"], reverse=True)
    for n, row in enumerate(ranked, 1):
        row["rank"] = n
    return {"generated": now_ms, "exchange": exchange, "market_type": "spot", "quote": cfg.quote,
            "config": asdict(cfg), "pairs": ranked, "excluded": excluded,
            "note": "Ranked on current public market data. Not a forecast or a profit signal; "
                    "backtesting today's top pairs over past years flatters results (survivorship bias)."}


def _ineligible(m: dict, cfg: RankConfig, now_ms: int) -> str | None:
    base = str(m.get("base") or "")
    if not m.get("spot") or m.get("quote") != cfg.quote:
        return "not a spot USDT pair"
    if m.get("active") is False:
        return "not trading (inactive)"
    if is_stablecoin(base):
        return "stablecoin against a stablecoin"
    if is_leveraged_token(base):
        return "leveraged token"
    listed = _listed_ms(m)
    if listed and now_ms - listed < cfg.min_listing_days * DAY_MS:
        return f"listed {(now_ms - listed) / DAY_MS:.0f} days ago (need {cfg.min_listing_days})"
    return None


def _listed_ms(m: dict) -> int | None:
    for v in (m.get("created"), (m.get("info") or {}).get("launchTime")):
        try:
            if v and int(v) > 0:
                return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _measure(sym: str, m: dict, t: dict, now_ms: int) -> dict:
    last = _num(t.get("last")) or _num(t.get("close"))
    bid, ask = _num(t.get("bid")), _num(t.get("ask"))
    turnover = _num(t.get("quoteVolume"))
    if turnover is None and last and _num(t.get("baseVolume")):
        turnover = last * _num(t.get("baseVolume"))
    hi, lo = _num(t.get("high")), _num(t.get("low"))
    spread = (ask - bid) / ((ask + bid) / 2) * 1e4 if bid and ask and ask >= bid else None
    listed = _listed_ms(m)
    return {"symbol": sym, "base": m.get("base"), "last": last, "turnover_usdt": turnover,
            "spread_bps": None if spread is None else round(spread, 2),
            "volatility_pct": round((hi - lo) / last * 100, 2) if hi and lo and last else None,
            "change_pct": _num(t.get("percentage")),
            "listed_days": round((now_ms - listed) / DAY_MS) if listed else None,
            "depth_usdt": None, "validated": None}


def _below_floor(row: dict, cfg: RankConfig) -> str | None:
    if not row["turnover_usdt"] or row["turnover_usdt"] < cfg.min_turnover_usdt:
        return f"24h turnover {_usdt(row['turnover_usdt'])} USDT under {_usdt(cfg.min_turnover_usdt)}"
    if row["spread_bps"] is None:
        return "no bid/ask quote"
    if row["spread_bps"] > cfg.max_spread_bps:
        return f"spread {row['spread_bps']:.1f} bps over {cfg.max_spread_bps:g}"
    if row["depth_usdt"] is not None and row["depth_usdt"] < cfg.min_depth_usdt:
        return (f"order book depth {_usdt(row['depth_usdt'])} USDT within {cfg.depth_pct:g}% of mid "
                f"under {_usdt(cfg.min_depth_usdt)}")
    return None


async def _add_depth(client, row: dict, cfg: RankConfig) -> None:
    try:
        book = await client.fetch_order_book(row["symbol"], 50)
    except Exception as exc:  # depth is a refinement: the pair keeps its ticker-based score
        log.warning("order book for %s failed: %s", row["symbol"], exc)
        return
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * (1 - cfg.depth_pct / 100), mid * (1 + cfg.depth_pct / 100)
    bid_usdt = sum(p * q for p, q, *_ in bids if p >= lo)
    ask_usdt = sum(p * q for p, q, *_ in asks if p <= hi)
    row["depth_usdt"] = round(min(bid_usdt, ask_usdt), 2)
    row["spread_bps"] = round((asks[0][0] - bids[0][0]) / mid * 1e4, 2)


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _parts(row: dict, cfg: RankConfig) -> dict:
    liq = _clip((math.log10(row["turnover_usdt"]) - 6) / 3)  # 1M -> 0, 1B -> 1
    spread = _clip(1 - (row["spread_bps"] or 0) / cfg.max_spread_bps)
    depth = 0.5 if row["depth_usdt"] is None else _clip((math.log10(max(row["depth_usdt"], 1)) - 4) / 3)
    v, (lo, hi) = row["volatility_pct"], cfg.vol_band
    if v is None:
        vol = 0.5
    elif v < lo:
        vol = _clip(v / lo)
    elif v > hi:
        vol = _clip(1 - (v - hi) / (2 * hi))  # very fast markets lose points
    else:
        vol = 1.0
    val = row.get("validated") or {}
    n, exp = val.get("trades", 0), val.get("expectancy_r", 0.0)
    proof = 0.5 if n < 30 else _clip(0.5 + exp * 2.5)  # too few trades: neutral
    return {"liquidity": liq, "spread": spread, "depth": depth, "volatility": vol, "validated": proof}


WEIGHTS = {"liquidity": 35, "spread": 20, "depth": 20, "volatility": 10, "validated": 15}


def _score(row: dict, cfg: RankConfig) -> float:
    p = _parts(row, cfg)
    return round(sum(WEIGHTS[k] * p[k] for k in WEIGHTS), 1)


def _reasons(row: dict, cfg: RankConfig) -> list[str]:
    out = [f"24h turnover {_usdt(row['turnover_usdt'])} USDT", f"spread {row['spread_bps']:.1f} bps"]
    out.append(f"depth {_usdt(row['depth_usdt'])} USDT within {cfg.depth_pct:g}% of mid"
               if row["depth_usdt"] is not None else "order book depth not checked")
    v = row["volatility_pct"]
    if v is not None:
        lo, hi = cfg.vol_band
        band = "in the preferred band" if lo <= v <= hi else "below the band" if v < lo else "fast: fewer points"
        out.append(f"24h range {v:.1f}% ({band})")
    val = row.get("validated")
    if val and val.get("trades"):
        out.append(f"platform results: {val['trades']} validated trades, {val['expectancy_r']:+.2f}R each"
                   + ("" if val["trades"] >= 30 else " (too few to count)"))
    else:
        out.append("no validated results on this pair yet")
    out.append(f"listed {row['listed_days']} days ago" if row["listed_days"] is not None
               else "listing age unknown")
    return out


def validated_results(registry_path: str | None, paper_state: str | None) -> dict:
    """Per symbol: out-of-sample trades of strategies that passed the backtest gate, plus closed
    paper trades. {symbol: {"trades", "expectancy_r", "oos_trades", "paper_trades"}}"""
    acc: dict = {}

    def add(sym, kind, n, total_r):
        a = acc.setdefault(sym, {"oos_trades": 0, "paper_trades": 0, "total_r": 0.0})
        a[kind] += n
        a["total_r"] += total_r

    for e in (_read(registry_path).get("strategies") or [] if registry_path else []):
        bt = (e.get("backtest") if isinstance(e, dict) else None) or {}
        if not bt.get("markets") or bt.get("failed"):
            continue  # only strategies that passed the gate count as validated
        for mk in bt["markets"]:
            n = int(mk.get("trades_oos") or 0)
            if n:
                add(mk["symbol"], "oos_trades", n, n * float(mk.get("exp_oos") or 0))
    for t in (_read(paper_state).get("trades", []) if paper_state else []):
        parts = str(t.get("key", "")).split("|")
        if len(parts) >= 4:
            add(parts[2], "paper_trades", 1, float(t.get("r") or 0))
    out = {}
    for sym, a in acc.items():
        n = a["oos_trades"] + a["paper_trades"]
        out[sym] = {"trades": n, "expectancy_r": round(a["total_r"] / n, 4) if n else 0.0,
                    "oos_trades": a["oos_trades"], "paper_trades": a["paper_trades"]}
    return out


def _read(path: str | None) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_ranking(ranking: dict, path: str = DEFAULT_PATH) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ranking, fh, indent=1)
    os.replace(tmp, path)
    return path


def load_ranking(path: str = DEFAULT_PATH) -> dict:
    return _read(path)


def ranked_symbols(path: str = DEFAULT_PATH, top: int = 20, max_age_hours: float = 48.0,
                   now_ms: int | None = None) -> list[str] | None:
    """The top ``top`` ranked symbols, or None when the ranking is missing, empty or stale."""
    rk = load_ranking(path)
    now_ms = now_ms or int(time.time() * 1000)
    if not rk.get("pairs") or not rk.get("generated"):
        return None
    if now_ms - int(rk["generated"]) > max_age_hours * 3_600_000:
        return None
    return [p["symbol"] for p in rk["pairs"][:max(1, top)]]


def configured_symbols(fallback: list[str], env=None, path: str | None = None,
                       now_ms: int | None = None, keep: list[str] | None = None,
                       market: str | None = None) -> list[str]:
    """The pairs to trade: the fixed list unless ``AITRADER_PAIRS=ranked`` and a fresh ranking
    exists, plus today's top gainers while that switch is on (``market.gainers``): the Spot
    scanner's for ``market="spot"``, the Futures scanner's for ``"futures"``, both by default.
    ``keep`` (e.g. pairs with open paper positions) is always added."""
    env = os.environ if env is None else env
    symbols = list(fallback)
    if (env.get("AITRADER_PAIRS") or "fixed").strip().lower() == "ranked":
        ranked = ranked_symbols(path or env.get("AITRADER_PAIRS_FILE") or DEFAULT_PATH,
                                top=int(env.get("AITRADER_PAIRS_TOP") or 20),
                                max_age_hours=float(env.get("AITRADER_PAIRS_MAX_AGE_HOURS") or 48),
                                now_ms=now_ms)
        if ranked:
            symbols = ranked
        else:
            log.warning("AITRADER_PAIRS=ranked but the pair ranking is missing or stale; "
                        "using the fixed list (run `aitrader pairs rank`)")
    from . import gainers  # the Pairs page's "Trade the top gainers" switch

    files = {"spot": env.get("AITRADER_SPOT_GAINERS_FILE") or gainers.SPOT_PATH,
             "futures": env.get("AITRADER_GAINERS_FILE") or gainers.DEFAULT_PATH}
    hot = [s for m in ((market,) if market in files else ("spot", "futures"))
           for s in gainers.symbols(files[m], now_ms=now_ms)]
    return list(dict.fromkeys([*symbols, *hot, *(keep or [])]))


def open_paper_symbols(state_path: str) -> list[str]:
    """Symbols with open paper positions: they keep getting candles after leaving the ranking."""
    keys = (_read(state_path).get("open") or {}).keys()
    return sorted({k.split("|")[2] for k in keys if k.count("|") >= 3})


def public_client(exchange_id: str = "bybit"):
    """A ccxt async client for public market data (no keys), spot markets by default."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def ranking_text(rk: dict, top: int = 30) -> str:
    """Plain-text table for the terminal."""
    from datetime import datetime, timezone

    when = datetime.fromtimestamp(rk["generated"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"{rk['exchange']} {rk['market_type']} {rk['quote']} pairs ranked {when} UTC: "
             f"{len(rk['pairs'])} eligible, {len(rk['excluded'])} excluded",
             f"{'#':>3} {'pair':14} {'score':>5} {'turnover':>9} {'spread':>7} {'depth':>8} {'range':>6}"]
    for p in rk["pairs"][:top]:
        lines.append(f"{p['rank']:>3} {p['symbol']:14} {p['score']:>5.1f} {_usdt(p['turnover_usdt']):>9} "
                     f"{p['spread_bps']:>6.1f}b {_usdt(p['depth_usdt']):>8} "
                     f"{(p['volatility_pct'] or 0):>5.1f}%  {'; '.join(p['reasons'][4:])}")
    lines.append(rk.get("note", ""))
    return "\n".join(lines)


def _usdt(x) -> str:
    if x is None or not math.isfinite(x):
        return "?"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:.0f}"
