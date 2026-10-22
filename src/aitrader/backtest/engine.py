"""Bar-by-bar backtest of a strategy's signals with fees, slippage and (for perps) funding.

Long signals (``Side.BUY``) are spot trades. Short signals (``Side.SELL``) are USDT perpetual
futures trades on Bybit: perp fees, and a funding cost charged on every bar held. Funding is
usually paid *to* shorts, but it isn't counted as income here (it can turn), so the result is on
the cautious side. Signals and fills use the spot candles: the perp price tracks spot closely,
and the small gap between them is not modelled.

Fill rules, chosen to avoid flattering results (for a short, "below" and "above" swap):
- Entries fill at the *next* bar's open plus slippage, after the signal bar has closed.
- Stops and targets are checked against each later bar's high/low. If one bar touches both, the
  stop is assumed to fill first. A gap through the stop fills at the open (worse than the stop).
- Stops fill as taker with slippage; targets fill as maker at the target price.
- Trailing rules (close below EMA50, RSI above a level, time stop) exit at the next open.
- One open trade per strategy and symbol at a time; later signals are skipped while one is open.
- Fee filter: a signal whose stop is so close that the round trip's fees and slippage would cost
  more than ``max_cost_r`` of the risk is skipped (``cost_r``). On a 5m chart a 0.2% stop against
  a 0.3% round trip loses about 1R to costs alone, so such a trade can hardly win.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pandas as pd

from ..indicators import ema, parabolic_sar, rsi
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal, Strategy


@dataclass
class BacktestConfig:
    taker_fee: float = 0.001  # Bybit spot base rate (non-VIP) 0.1%
    maker_fee: float = 0.001
    slippage_bps: float = 5.0
    funding_rate_8h: float = 0.0  # set e.g. 0.0001 to charge perps funding on longs
    perp_taker_fee: float = 0.00055  # Bybit USDT perpetual base rates (non-VIP)
    perp_maker_fee: float = 0.0002
    perp_funding_8h: float = 0.0001  # charged to shorts as a cost, never counted as income
    max_bars_in_trade: int = 10_000
    max_cost_r: float = 0.25  # skip signals whose round-trip costs exceed this share of the risk (0: off)
    min_rr: float = 0.0  # skip signals whose fixed target is less than this many R away (0: off)
    max_rr: float = 0.0  # pull a fixed target further than this many R away in to it (0: off)
    split_tp: bool = False  # take half the position at halfway to the target, the rest at the target
    breakeven: bool = False  # with split_tp: once the first half is taken, the stop moves to breakeven
    maker_entry: bool = False  # enter with a post-only limit order (maker fee), or not at all (maker_fill)


@dataclass
class Trade:
    strategy: str
    symbol: str
    timeframe: str
    signal_ts: int
    entry_ts: int
    entry: float
    stop: float
    target: float | None
    exit_ts: int = 0
    exit: float = 0.0
    exit_reason: str = ""
    r: float = 0.0  # net result in R after all costs
    gross_r: float = 0.0
    cost_r: float = 0.0  # fees + funding (slippage is already in the fill prices)
    fee_r: float = 0.0
    slippage_r: float = 0.0  # what slippage took from the fills, in R
    funding_r: float = 0.0
    tp1: float | None = None  # with split_tp: the price where the first half was taken, if it was
    features: dict = field(default_factory=dict)


# Exit rules registered by strategies (e.g. rule-language "exit.when" conditions), looked up by
# the key that appears in Signal.trailing.
CUSTOM_EXITS: dict = {}


def _trailing_exit(rule: str | None, df: pd.DataFrame):
    """Return a boolean Series: True on bars whose close triggers the trailing exit. Several
    rules can be joined with "|" (any of them exits); "time_stop=N" parts are handled separately."""
    if not rule:
        return None
    out = None
    for part in rule.split("|"):
        hit = _one_trailing_exit(part.strip(), df)
        if hit is not None:
            out = hit if out is None else (out | hit)
    return out


def _one_trailing_exit(rule: str, df: pd.DataFrame):
    if rule in CUSTOM_EXITS:
        return CUSTOM_EXITS[rule](df).astype(bool)
    if rule.startswith("close<ema"):
        n = int(rule.split("ema")[1])
        return df["close"] < ema(df["close"], n)
    if rule.startswith("rsi>"):
        return rsi(df["close"]) > float(rule[4:])
    if rule == "close<psar":
        return df["close"] < parabolic_sar(df)["sar"]
    if rule.startswith("flipped:"):  # a short twin's exit, also before the twin registered it (a restart)
        from ..strategies.mirror import flip

        return _one_trailing_exit(rule[len("flipped:"):], flip(df))
    if rule.startswith("when:"):  # a strategy's own exit rule that was never registered
        raise KeyError(f"exit rule {rule} is not registered (build the strategy first)")
    return None


def _time_stop(rule: str | None) -> int | None:
    for part in (rule or "").split("|"):
        part = part.strip()
        if part.startswith("time_stop="):
            return int(part.split("=")[1])
    return None


def simulate_signal(sig: Signal, df: pd.DataFrame, pos: int, cfg: BacktestConfig,
                    trail_cache: dict) -> Trade | None:
    """Simulate one signal; ``pos`` is the index of the bar after the signal bar."""
    if pos >= len(df):
        return None
    sig = capped(sig, cfg.max_rr)
    if sig.side is Side.SELL:
        return _simulate_short(sig, df, pos, cfg, trail_cache)
    slip = cfg.slippage_bps / 10_000
    o, h, lo, c, ts = (df[k].to_numpy() for k in ("open", "high", "low", "close", "ts"))
    maker = maker_fill(sig, df, pos) if cfg.maker_entry else None
    if cfg.maker_entry and maker is None:
        return None  # the limit order was never filled
    entry = maker if maker is not None else o[pos] * (1 + slip)
    entry_fee = cfg.maker_fee if maker is not None else cfg.taker_fee
    risk = sig.entry - sig.stop  # per-unit risk as planned at the signal
    if risk <= 0 or entry <= sig.stop:
        return None  # gapped through the stop before we could enter
    target = sig.target
    tp1 = _tp1(sig, cfg)
    half = None  # where the first half was taken (split take-profit)
    trail = None
    if sig.trailing not in trail_cache:
        trail_cache[sig.trailing] = _trailing_exit(sig.trailing, df)
    trail = trail_cache[sig.trailing]
    trail_arr = trail.to_numpy() if trail is not None else None
    tstop = _time_stop(sig.trailing)

    exit_px, exit_reason, exit_i, exit_fee = None, "", None, cfg.taker_fee
    end = min(len(df), pos + cfg.max_bars_in_trade)
    stop, why = sig.stop, "stop"
    # breakeven: the price where the second half's exit pays back both fees and the slippage
    be = entry * (1 + entry_fee + cfg.taker_fee + slip) / (1 - slip)
    for i in range(pos, end):
        # a limit entry fills somewhere inside its candle: a target touched in that same candle may
        # have come before the fill, so it isn't counted (the stop still is: the cautious reading)
        limit_bar = maker is not None and i == pos
        # gap through stop at the open
        if i > pos and o[i] <= stop:
            exit_px, exit_reason, exit_i = o[i] * (1 - slip), why + "_gap", i
            break
        if lo[i] <= stop:
            exit_px, exit_reason, exit_i = stop * (1 - slip), why, i
            break
        if tp1 is not None and half is None and h[i] >= tp1 and not limit_bar:
            half = tp1
            if cfg.breakeven and stop < be < tp1:  # from the next candle on, the rest can't lose
                stop, why = be, "breakeven"
        if target is not None and h[i] >= target and not limit_bar:
            exit_px, exit_reason, exit_i, exit_fee = target, "target", i, cfg.maker_fee
            break
        held = i - pos + 1
        if (trail_arr is not None and bool(trail_arr[i])) or (tstop and held >= tstop):
            if i + 1 < len(df):
                exit_px, exit_reason, exit_i = o[i + 1] * (1 - slip), "trail" if trail_arr is not None and trail_arr[i] else "time", i + 1
            else:
                exit_px, exit_reason, exit_i = c[i], "end", i
            break
    if exit_px is None:
        exit_i = end - 1
        exit_px, exit_reason = c[exit_i], "end"

    bars = exit_i - pos + 1
    tf_hours = TIMEFRAME_MS[sig.timeframe] / 3_600_000
    funding = cfg.funding_rate_8h * bars * tf_hours / 8 * entry
    taken = 0.5 if half is not None else 0.0  # the share closed early at tp1 (as maker)
    fees = entry * entry_fee + taken * _half_or(half) * cfg.maker_fee + (1 - taken) * exit_px * exit_fee
    # slipped exits were priced at (1 - slip) times the quoted price; targets and "end" were not
    slipped_exit = exit_reason in ("stop", "stop_gap", "breakeven", "breakeven_gap", "trail", "time")
    slippage = (0.0 if maker is not None else entry - o[pos]) + (1 - taken) * (exit_px / (1 - slip) - exit_px if slipped_exit and slip < 1 else 0.0)
    gross = taken * (_half_or(half) - entry) + (1 - taken) * (exit_px - entry)
    net = gross - fees - funding
    return Trade(
        strategy=sig.strategy, symbol=sig.symbol, timeframe=sig.timeframe, signal_ts=sig.ts,
        entry_ts=int(ts[pos]), entry=entry, stop=sig.stop, target=target,
        exit_ts=int(ts[exit_i]), exit=float(exit_px), exit_reason=exit_reason,
        r=net / risk, gross_r=gross / risk, cost_r=(fees + funding) / risk,
        fee_r=fees / risk, slippage_r=slippage / risk, funding_r=funding / risk, tp1=half,
        features=dict(sig.features),
    )


def maker_fill(sig: Signal, df: pd.DataFrame, pos: int) -> float | None:
    """A post-only limit entry: a buy is placed at the signal price or the entry candle's open,
    whichever is lower (a sell at the higher), and fills only if that candle trades through it
    (its low goes below the price; for a sell, its high above). Returns the fill price, or None
    when price ran away and the order is cancelled at the candle's close: the trade is missed."""
    if pos >= len(df):
        return None
    o, h, lo = float(df["open"].iloc[pos]), float(df["high"].iloc[pos]), float(df["low"].iloc[pos])
    if sig.side is Side.SELL:
        price = max(sig.entry, o)
        return price if h > price else None
    price = min(sig.entry, o)
    return price if lo < price else None


def _tp1(sig: Signal, cfg: BacktestConfig) -> float | None:
    """With the split take-profit on, the first target: halfway from the signal price to the target."""
    if not cfg.split_tp or sig.target is None:
        return None
    return sig.entry + (sig.target - sig.entry) / 2


def _half_or(half: float | None) -> float:
    return half if half is not None else 0.0


def _simulate_short(sig: Signal, df: pd.DataFrame, pos: int, cfg: BacktestConfig,
                    trail_cache: dict) -> Trade | None:
    """The mirror of the long rules: sell at the next open minus slippage, buy back at the stop
    (above), the target (below), a trailing rule or the time stop."""
    slip = cfg.slippage_bps / 10_000
    o, h, lo, c, ts = (df[k].to_numpy() for k in ("open", "high", "low", "close", "ts"))
    maker = maker_fill(sig, df, pos) if cfg.maker_entry else None
    if cfg.maker_entry and maker is None:
        return None  # the limit order was never filled
    entry = maker if maker is not None else o[pos] * (1 - slip)
    entry_fee = cfg.perp_maker_fee if maker is not None else cfg.perp_taker_fee
    risk = sig.stop - sig.entry
    if risk <= 0 or entry >= sig.stop:
        return None  # gapped through the stop before we could enter
    target = sig.target
    tp1 = _tp1(sig, cfg)
    half = None
    if sig.trailing not in trail_cache:
        trail_cache[sig.trailing] = _trailing_exit(sig.trailing, df)
    trail = trail_cache[sig.trailing]
    trail_arr = trail.to_numpy() if trail is not None else None
    tstop = _time_stop(sig.trailing)

    exit_px, exit_reason, exit_i, exit_fee = None, "", None, cfg.perp_taker_fee
    end = min(len(df), pos + cfg.max_bars_in_trade)
    stop, why = sig.stop, "stop"
    be = entry * (1 - entry_fee - cfg.perp_taker_fee - slip) / (1 + slip)
    for i in range(pos, end):
        # a limit entry fills somewhere inside its candle: a target touched in that same candle may
        # have come before the fill, so it isn't counted (the stop still is: the cautious reading)
        limit_bar = maker is not None and i == pos
        if i > pos and o[i] >= stop:
            exit_px, exit_reason, exit_i = o[i] * (1 + slip), why + "_gap", i
            break
        if h[i] >= stop:
            exit_px, exit_reason, exit_i = stop * (1 + slip), why, i
            break
        if tp1 is not None and half is None and lo[i] <= tp1 and not limit_bar:
            half = tp1
            if cfg.breakeven and tp1 < be < stop:
                stop, why = be, "breakeven"
        if target is not None and lo[i] <= target and not limit_bar:
            exit_px, exit_reason, exit_i, exit_fee = target, "target", i, cfg.perp_maker_fee
            break
        held = i - pos + 1
        if (trail_arr is not None and bool(trail_arr[i])) or (tstop and held >= tstop):
            if i + 1 < len(df):
                exit_px, exit_reason, exit_i = (o[i + 1] * (1 + slip),
                                                "trail" if trail_arr is not None and trail_arr[i] else "time", i + 1)
            else:
                exit_px, exit_reason, exit_i = c[i], "end", i
            break
    if exit_px is None:
        exit_i = end - 1
        exit_px, exit_reason = c[exit_i], "end"

    bars = exit_i - pos + 1
    tf_hours = TIMEFRAME_MS[sig.timeframe] / 3_600_000
    funding = cfg.perp_funding_8h * bars * tf_hours / 8 * entry
    taken = 0.5 if half is not None else 0.0
    fees = entry * entry_fee + taken * _half_or(half) * cfg.perp_maker_fee + (1 - taken) * exit_px * exit_fee
    slipped_exit = exit_reason in ("stop", "stop_gap", "breakeven", "breakeven_gap", "trail", "time")
    slippage = (0.0 if maker is not None else o[pos] - entry) + (1 - taken) * (exit_px - exit_px / (1 + slip) if slipped_exit else 0.0)
    gross = taken * (entry - _half_or(half)) + (1 - taken) * (entry - exit_px)
    net = gross - fees - funding
    return Trade(
        strategy=sig.strategy, symbol=sig.symbol, timeframe=sig.timeframe, signal_ts=sig.ts,
        entry_ts=int(ts[pos]), entry=entry, stop=sig.stop, target=target,
        exit_ts=int(ts[exit_i]), exit=float(exit_px), exit_reason=exit_reason,
        r=net / risk, gross_r=gross / risk, cost_r=(fees + funding) / risk,
        fee_r=fees / risk, slippage_r=slippage / risk, funding_r=funding / risk, tp1=half,
        features=dict(sig.features),
    )


def cost_r(sig: Signal, cfg: BacktestConfig) -> float:
    """The round trip's fees and slippage as a share of the planned risk, assuming the trade
    exits at its stop as taker (the costly case). ``inf`` when the stop is on the wrong side."""
    risk = abs(sig.entry - sig.stop)
    if risk <= 0 or sig.entry <= 0:
        return float("inf")
    fee = cfg.perp_taker_fee if sig.side is Side.SELL else cfg.taker_fee
    slip = cfg.slippage_bps / 10_000
    return (sig.entry + sig.stop) * (fee + slip) / risk


def as_perp(cfg: BacktestConfig) -> BacktestConfig:
    """The same settings with longs costed as USDT perpetual futures (perp fees and funding)."""
    from dataclasses import replace

    return replace(cfg, taker_fee=cfg.perp_taker_fee, maker_fee=cfg.perp_maker_fee,
                   funding_rate_8h=cfg.perp_funding_8h)


def capped(sig: Signal, max_rr: float) -> Signal:
    """The signal with its fixed target pulled in to ``max_rr`` times the risk when it is further
    (0: off). A signal with no fixed target keeps exiting on its own rule."""
    rr = sig.r_multiple_target
    if not max_rr or rr is None or rr <= max_rr + 1e-9:
        return sig
    risk = abs(sig.entry - sig.stop)
    return replace(sig, target=sig.entry - max_rr * risk if sig.side is Side.SELL else sig.entry + max_rr * risk)


def too_costly(sig: Signal, cfg: BacktestConfig) -> bool:
    return bool(cfg.max_cost_r) and cost_r(sig, cfg) > cfg.max_cost_r


def too_little_reward(sig: Signal, cfg: BacktestConfig) -> bool:
    """With a minimum reward set: the signal's fixed target is closer than ``min_rr`` times the risk.
    A signal with no fixed target (it exits on its own rule) is not judged."""
    rr = sig.r_multiple_target
    return bool(cfg.min_rr) and rr is not None and rr < cfg.min_rr - 1e-9


def run_backtest(strategy: Strategy, df: pd.DataFrame, exchange: str, symbol: str,
                 timeframe: str, cfg: BacktestConfig | None = None, costly: list | None = None) -> list[Trade]:
    """``costly``: when given, the signals the fee filter skipped are appended to it."""
    cfg = cfg or BacktestConfig()
    signals = strategy.generate(df, exchange, symbol, timeframe)
    step = TIMEFRAME_MS[timeframe]
    index_of = {int(t): i for i, t in enumerate(df["ts"].to_numpy())}
    trades: list[Trade] = []
    busy_until = -1
    cache: dict = {}
    for sig in signals:
        pos = index_of.get(sig.ts)  # bar opening at the signal bar's close = next bar
        if pos is None:
            pos = index_of.get(sig.ts - step)
            pos = None if pos is None else pos + 1
        if pos is None or pos <= busy_until:
            continue
        if too_costly(sig, cfg):
            if costly is not None:
                costly.append(sig)
            continue
        if too_little_reward(sig, cfg):
            continue
        t = simulate_signal(sig, df, pos, cfg, cache)
        if t is None:
            continue
        trades.append(t)
        busy_until = index_of[t.exit_ts]
    return trades


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["strategy", "symbol", "timeframe", "entry_ts", "exit_ts", "r"])
    df = pd.DataFrame([t.__dict__ for t in trades]).drop(columns=["features"])
    df["exit_time"] = pd.to_datetime(df["exit_ts"], unit="ms", utc=True)
    return df
