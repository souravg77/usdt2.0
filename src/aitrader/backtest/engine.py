"""Bar-by-bar backtest of a strategy's signals with fees, slippage and (for perps) funding.

Fill rules, chosen to avoid flattering results:
- Entries fill at the *next* bar's open plus slippage, after the signal bar has closed.
- Stops and targets are checked against each later bar's high/low. If one bar touches both, the
  stop is assumed to fill first. A gap through the stop fills at the open (worse than the stop).
- Stops fill as taker with slippage; targets fill as maker at the target price.
- Trailing rules (close below EMA50, RSI above a level, time stop) exit at the next open.
- One open trade per strategy and symbol at a time; later signals are skipped while one is open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..indicators import ema, parabolic_sar, rsi
from ..models import TIMEFRAME_MS
from ..strategies.base import Signal, Strategy


@dataclass
class BacktestConfig:
    taker_fee: float = 0.001  # Bybit spot base rate (non-VIP) 0.1%
    maker_fee: float = 0.001
    slippage_bps: float = 5.0
    funding_rate_8h: float = 0.0  # set e.g. 0.0001 to charge perps funding on longs
    max_bars_in_trade: int = 10_000


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
    slip = cfg.slippage_bps / 10_000
    o, h, lo, c, ts = (df[k].to_numpy() for k in ("open", "high", "low", "close", "ts"))
    entry = o[pos] * (1 + slip)
    risk = sig.entry - sig.stop  # per-unit risk as planned at the signal
    if risk <= 0 or entry <= sig.stop:
        return None  # gapped through the stop before we could enter
    target = sig.target
    trail = None
    if sig.trailing not in trail_cache:
        trail_cache[sig.trailing] = _trailing_exit(sig.trailing, df)
    trail = trail_cache[sig.trailing]
    trail_arr = trail.to_numpy() if trail is not None else None
    tstop = _time_stop(sig.trailing)

    exit_px, exit_reason, exit_i, exit_fee = None, "", None, cfg.taker_fee
    end = min(len(df), pos + cfg.max_bars_in_trade)
    for i in range(pos, end):
        # gap through stop at the open
        if i > pos and o[i] <= sig.stop:
            exit_px, exit_reason, exit_i = o[i] * (1 - slip), "stop_gap", i
            break
        if lo[i] <= sig.stop:
            exit_px, exit_reason, exit_i = sig.stop * (1 - slip), "stop", i
            break
        if target is not None and h[i] >= target:
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
    fees = entry * cfg.taker_fee + exit_px * exit_fee
    # slipped exits were priced at (1 - slip) times the quoted price; targets and "end" were not
    slipped_exit = exit_reason in ("stop", "stop_gap", "trail", "time")
    slippage = (entry - o[pos]) + (exit_px / (1 - slip) - exit_px if slipped_exit and slip < 1 else 0.0)
    gross = exit_px - entry
    net = gross - fees - funding
    return Trade(
        strategy=sig.strategy, symbol=sig.symbol, timeframe=sig.timeframe, signal_ts=sig.ts,
        entry_ts=int(ts[pos]), entry=entry, stop=sig.stop, target=target,
        exit_ts=int(ts[exit_i]), exit=float(exit_px), exit_reason=exit_reason,
        r=net / risk, gross_r=gross / risk, cost_r=(fees + funding) / risk,
        fee_r=fees / risk, slippage_r=slippage / risk, funding_r=funding / risk,
        features=dict(sig.features),
    )


def run_backtest(strategy: Strategy, df: pd.DataFrame, exchange: str, symbol: str,
                 timeframe: str, cfg: BacktestConfig | None = None) -> list[Trade]:
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
