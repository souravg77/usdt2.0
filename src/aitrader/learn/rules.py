"""A small, safe rule language for strategies the agent writes itself.

A book-derived strategy is plain JSON (a "rule spec"). Nothing in it is executed as code: every
feature name, operator and number is checked against the whitelist below, and anything else is
rejected with a readable error. The language can only describe long, spot entries with a stop,
so it cannot express leverage or shorting.

Example::

    {
      "name": "rsi_oversold_bounce",
      "description": "Buy oversold dips inside an uptrend",
      "source": {"book": "Binance Academy trading eBook", "pages": "p. 53", "quote": "..."},
      "timeframes": ["1h", "4h"],
      "entry": [
        {"left": "close", "op": ">", "right": "ema(200)"},
        {"left": "rsi(14)", "op": "crosses_above", "right": 30}
      ],
      "stop": {"type": "atr", "mult": 2},
      "target": {"r": 2},
      "exit": {"trailing": "rsi>70", "time_stop_bars": 48},
      "decisions": ["'Oversold' = RSI(14) below 30, as the book says"],
      "uncodable": []
    }

Feature expressions look like ``rsi(14)``, ``bb_upper(20,2)``, ``close[1]`` (one bar ago) or
``1.5*avg_volume(20)``. See ``FEATURES`` for the full list.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..indicators import (
    adx,
    atr,
    atr_percentile,
    bollinger,
    ema,
    ema_stack,
    ichimoku,
    macd,
    market_structure,
    parabolic_sar,
    rsi,
    sma,
    stoch_rsi,
    vwap,
)
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal, Strategy, StrategySpec

# name -> (defaults for each argument, help text). Every argument is a number.
FEATURES: dict[str, tuple[tuple[float, ...], str]] = {
    "close": ((), "bar close"),
    "open": ((), "bar open"),
    "high": ((), "bar high"),
    "low": ((), "bar low"),
    "volume": ((), "bar volume"),
    "sma": ((20,), "simple moving average of close"),
    "ema": ((20,), "exponential moving average of close"),
    "rsi": ((14,), "RSI 0-100"),
    "stoch_rsi": ((14,), "Stochastic RSI 0-1"),
    "macd": ((12, 26, 9), "MACD line"),
    "macd_signal": ((12, 26, 9), "MACD signal line"),
    "macd_hist": ((12, 26, 9), "MACD histogram"),
    "atr": ((14,), "average true range"),
    "atr_pct": ((14,), "ATR percentile over the last 500 bars, 0-1"),
    "adx": ((14,), "trend strength 0-100"),
    "bb_upper": ((20, 2), "upper Bollinger band"),
    "bb_lower": ((20, 2), "lower Bollinger band"),
    "bb_mid": ((20, 2), "middle Bollinger band"),
    "bb_width": ((20, 2), "band width / middle band"),
    "bb_width_rank": ((20, 2, 120), "percentile of band width over the last N bars, 0-1"),
    "vwap": ((20,), "rolling volume-weighted average price"),
    "psar": ((), "Parabolic SAR level"),
    "highest_high": ((20,), "highest high of the previous N bars (excludes this bar)"),
    "lowest_low": ((20,), "lowest low of the previous N bars (excludes this bar)"),
    "avg_volume": ((20,), "average volume of the previous N bars"),
    "rel_volume": ((20,), "volume / average volume of the previous N bars"),
    "change": ((1,), "close / close N bars ago - 1"),
    "tenkan": ((), "Ichimoku conversion line"),
    "kijun": ((), "Ichimoku base line"),
    "cloud_top": ((), "top of the Ichimoku cloud"),
    "cloud_bottom": ((), "bottom of the Ichimoku cloud"),
    "trend_structure": ((), "+1 higher highs and lows, -1 lower, 0 mixed"),
    "ema_stack": ((20, 50, 200), "+1 EMAs stacked up, -1 down, 0 mixed"),
}
OPS = (">", "<", ">=", "<=", "crosses_above", "crosses_below")
TRAILING = re.compile(r"^(close<ema\d{1,3}|close<psar|rsi>\d{2}(\.\d+)?)$")
STOP_TYPES = ("atr", "percent", "lowest_low", "indicator")
ALLOWED_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")
MAX_RISK_PCT = 0.01
_EXPR = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*\*\s*)?([a-z_]+)\s*(?:\(([^)]*)\))?\s*(?:\[(\d+)\])?\s*$")


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Expr:
    name: str
    args: tuple[float, ...]
    shift: int = 0
    mult: float = 1.0

    def key(self) -> tuple:
        return (self.name, self.args)


def parse_expr(text) -> Expr | float:
    if isinstance(text, bool):
        raise RuleError(f"not a number or feature: {text!r}")
    if isinstance(text, (int, float)):
        if not np.isfinite(text):
            raise RuleError(f"number must be finite: {text!r}")
        return float(text)
    if not isinstance(text, str) or len(text) > 60:
        raise RuleError(f"not a feature expression: {text!r}")
    try:
        return float(text)
    except ValueError:
        pass
    m = _EXPR.match(text.lower())
    if not m:
        raise RuleError(f"can't read {text!r}; use e.g. rsi(14), close[1] or 1.5*avg_volume(20)")
    mult, name, argtext, shift = m.groups()
    if name not in FEATURES:
        raise RuleError(f"unknown feature {name!r}; allowed: {', '.join(FEATURES)}")
    defaults = FEATURES[name][0]
    raw = [a.strip() for a in argtext.split(",")] if argtext and argtext.strip() else []
    if len(raw) > len(defaults):
        raise RuleError(f"{name} takes at most {len(defaults)} argument(s), got {len(raw)}")
    try:
        args = [float(a) for a in raw]
    except ValueError as exc:
        raise RuleError(f"arguments of {name} must be numbers: {text!r}") from exc
    for a in args:
        if not 0 < a <= 500:
            raise RuleError(f"argument {a:g} in {text!r} must be between 0 and 500")
    args += list(defaults[len(args):])
    shift_n = int(shift or 0)
    if shift_n > 50:
        raise RuleError(f"look-back [{shift_n}] in {text!r} is more than 50 bars")
    m_val = float(mult) if mult else 1.0
    if not 0 < m_val <= 100:
        raise RuleError(f"multiplier in {text!r} must be between 0 and 100")
    return Expr(name, tuple(args), shift_n, m_val)


def _num(d: dict, key: str, lo: float, hi: float, where: str, default=None) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise RuleError(f"{where}.{key} must be a number from {lo:g} to {hi:g}, got {v!r}")
    return float(v)


def validate_spec(spec: dict) -> dict:
    """Check a rule spec and return a cleaned copy. Raises RuleError listing every problem."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        raise RuleError("a strategy must be a JSON object")
    out: dict = {}
    name = str(spec.get("name", "")).strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")[:40]
    if not name:
        errors.append("name is missing")
    out["name"] = name
    out["description"] = str(spec.get("description", ""))[:500]
    src = spec.get("source") or {}
    out["source"] = {k: str(src.get(k, ""))[:400] for k in ("book", "pages", "quote")} \
        if isinstance(src, dict) else {"book": str(src)[:400], "pages": "", "quote": ""}

    side = str(spec.get("side", "long")).lower()
    if side not in ("long", "buy"):
        errors.append("only long (buy) strategies are allowed in the pilot: no shorting")
    for banned in ("leverage", "margin", "short"):
        if spec.get(banned):
            errors.append(f"{banned} is not allowed in the pilot")

    tfs = spec.get("timeframes") or []
    if not isinstance(tfs, list) or not tfs:
        errors.append("timeframes must be a non-empty list")
        tfs = []
    bad_tf = [t for t in tfs if t not in ALLOWED_TIMEFRAMES]
    if bad_tf:
        errors.append(f"timeframes {bad_tf} not allowed; use {', '.join(ALLOWED_TIMEFRAMES)}")
    out["timeframes"] = [t for t in tfs if t in ALLOWED_TIMEFRAMES][:3]

    conds = spec.get("entry")
    if not isinstance(conds, list) or not conds:
        errors.append("entry must be a non-empty list of conditions")
        conds = []
    if len(conds) > 8:
        errors.append("entry has more than 8 conditions")
    clean_conds = []
    for i, c in enumerate(conds[:8]):
        where = f"entry[{i}]"
        try:
            if not isinstance(c, dict):
                raise RuleError(f"{where} must be an object with left, op, right")
            if c.get("op") not in OPS:
                raise RuleError(f"{where}.op must be one of {OPS}, got {c.get('op')!r}")
            left, right = parse_expr(c.get("left")), parse_expr(c.get("right"))
            if not isinstance(left, Expr):
                raise RuleError(f"{where}.left must be a feature, not a number")
            within = int(c.get("within", 1) or 1)
            if not 1 <= within <= 20:
                raise RuleError(f"{where}.within must be 1-20 bars")
            clean_conds.append({"left": c["left"], "op": c["op"], "right": c["right"],
                                "within": within})
        except RuleError as exc:
            errors.append(str(exc))
        except (TypeError, ValueError) as exc:
            errors.append(f"{where}: {exc}")
    out["entry"] = clean_conds

    stop = spec.get("stop") or {}
    try:
        if not isinstance(stop, dict) or stop.get("type") not in STOP_TYPES:
            raise RuleError(f"stop.type must be one of {STOP_TYPES}; every trade needs a stop")
        t = stop["type"]
        if t == "atr":
            out["stop"] = {"type": t, "mult": _num(stop, "mult", 0.25, 10, "stop", 2.0),
                           "n": int(_num(stop, "n", 2, 200, "stop", 14))}
        elif t == "percent":
            out["stop"] = {"type": t, "pct": _num(stop, "pct", 0.002, 0.25, "stop")}
        elif t == "lowest_low":
            out["stop"] = {"type": t, "n": int(_num(stop, "n", 2, 200, "stop", 10))}
        else:
            e = parse_expr(stop.get("expr"))
            if not isinstance(e, Expr):
                raise RuleError("stop.expr must be a feature such as bb_mid(20,2)")
            out["stop"] = {"type": t, "expr": stop["expr"]}
    except RuleError as exc:
        errors.append(str(exc))

    target = spec.get("target")
    ex = spec.get("exit") or {}
    trailing = ex.get("trailing") if isinstance(ex, dict) else None
    time_stop = ex.get("time_stop_bars") if isinstance(ex, dict) else None
    if target is not None:
        try:
            out["target"] = {"r": _num(target if isinstance(target, dict) else {"r": target},
                                       "r", 0.5, 10, "target")}
        except RuleError as exc:
            errors.append(str(exc))
    else:
        out["target"] = None
    if trailing is not None and not (isinstance(trailing, str) and TRAILING.match(trailing)):
        errors.append("exit.trailing must be close<emaN, close<psar or rsi>X")
        trailing = None
    if time_stop is not None:
        if isinstance(time_stop, bool) or not isinstance(time_stop, int) or not 1 <= time_stop <= 1000:
            errors.append("exit.time_stop_bars must be a whole number from 1 to 1000")
            time_stop = None
    if out.get("target") is None and not trailing and not time_stop:
        errors.append("give a target, a trailing exit or a time stop so trades can end")
    out["exit"] = {"trailing": trailing, "time_stop_bars": time_stop}

    risk = spec.get("risk_pct", 0.005)
    if isinstance(risk, bool) or not isinstance(risk, (int, float)) or risk <= 0:
        risk = 0.005
    out["risk_pct"] = min(float(risk), MAX_RISK_PCT)
    out["decisions"] = [str(d)[:300] for d in (spec.get("decisions") or [])][:12]
    out["uncodable"] = [str(d)[:300] for d in (spec.get("uncodable") or [])][:12]
    if errors:
        raise RuleError("; ".join(errors))
    return out


def spec_id(spec: dict) -> str:
    """Stable id: the name plus a short hash of the trading rules (not the prose)."""
    core = {k: spec[k] for k in ("timeframes", "entry", "stop", "target", "exit")}
    digest = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:6]
    return f"{spec['name']}-{digest}"


# ---- evaluation ---------------------------------------------------------------------------------


def _feature(df: pd.DataFrame, e: Expr, cache: dict) -> pd.Series:
    k = e.key()
    if k not in cache:
        a = [int(x) if float(x).is_integer() else x for x in e.args]
        close = df["close"]
        n = e.name
        if n in ("close", "open", "high", "low", "volume"):
            s = df[n]
        elif n == "sma":
            s = sma(close, a[0])
        elif n == "ema":
            s = ema(close, a[0])
        elif n == "rsi":
            s = rsi(close, a[0])
        elif n == "stoch_rsi":
            s = stoch_rsi(close, a[0])["stoch_rsi"]
        elif n.startswith("macd"):
            m = macd(close, a[0], a[1], a[2])
            s = m[{"macd": "macd", "macd_signal": "signal", "macd_hist": "hist"}[n]]
        elif n == "atr":
            s = atr(df, a[0])
        elif n == "atr_pct":
            s = atr_percentile(df, a[0])
        elif n == "adx":
            s = adx(df, a[0])
        elif n.startswith("bb_"):
            bb = bollinger(close, a[0], float(e.args[1]))
            if n == "bb_width_rank":
                s = bb["width"].rolling(a[2], min_periods=a[2]).rank(pct=True)
            else:
                s = bb[n[3:] if n != "bb_width" else "width"]
        elif n == "vwap":
            s = vwap(df, a[0])
        elif n == "psar":
            s = parabolic_sar(df)["sar"]
        elif n == "highest_high":
            s = df["high"].shift(1).rolling(a[0], min_periods=a[0]).max()
        elif n == "lowest_low":
            s = df["low"].shift(1).rolling(a[0], min_periods=a[0]).min()
        elif n == "avg_volume":
            s = df["volume"].shift(1).rolling(a[0], min_periods=a[0]).mean()
        elif n == "rel_volume":
            s = df["volume"] / df["volume"].shift(1).rolling(a[0], min_periods=a[0]).mean()
        elif n == "change":
            s = close / close.shift(a[0]) - 1
        elif n in ("tenkan", "kijun"):
            s = ichimoku(df)[n]
        elif n in ("cloud_top", "cloud_bottom"):
            ich = ichimoku(df)
            pair = pd.concat([ich["span_a"], ich["span_b"]], axis=1)
            s = pair.max(axis=1, skipna=False) if n == "cloud_top" else pair.min(axis=1, skipna=False)
        elif n == "trend_structure":
            s = market_structure(df).astype(float)
        elif n == "ema_stack":
            s = ema_stack(close, tuple(a)).astype(float)
        else:  # pragma: no cover - parse_expr already rejects unknown names
            raise RuleError(f"unknown feature {n}")
        cache[k] = s.astype(float)
    s = cache[k]
    if e.shift:
        s = s.shift(e.shift)
    return s * e.mult if e.mult != 1.0 else s


def _value(df, x, cache):
    return _feature(df, x, cache) if isinstance(x, Expr) else pd.Series(x, index=df.index)


def condition_series(df: pd.DataFrame, cond: dict, cache: dict) -> pd.Series:
    left = _value(df, parse_expr(cond["left"]), cache)
    right = _value(df, parse_expr(cond["right"]), cache)
    op = cond["op"]
    if op == ">":
        hit = left > right
    elif op == "<":
        hit = left < right
    elif op == ">=":
        hit = left >= right
    elif op == "<=":
        hit = left <= right
    elif op == "crosses_above":
        hit = (left > right) & (left.shift(1) <= right.shift(1))
    else:
        hit = (left < right) & (left.shift(1) >= right.shift(1))
    hit = hit & left.notna() & right.notna()
    within = int(cond.get("within", 1))
    if within > 1:
        hit = hit.astype(int).rolling(within, min_periods=1).max() > 0
    return hit


class RuleStrategy(Strategy):
    """A strategy built from a validated rule spec."""

    def __init__(self, spec: dict, **params):
        self.rule = validate_spec(spec)
        self.spec = StrategySpec(
            name=self.rule["name"],
            source=" ".join(x for x in (self.rule["source"]["book"], self.rule["source"]["pages"]) if x),
            timeframes=tuple(self.rule["timeframes"]),
            setup=self.rule["description"],
            entry=" AND ".join(_cond_text(c) for c in self.rule["entry"]),
            stop=_stop_text(self.rule["stop"]),
            target=f"{self.rule['target']['r']:g}R" if self.rule["target"] else "none (exit rule only)",
            filter="; ".join(self.rule["uncodable"]) or "-",
            size=f"{self.rule['risk_pct']:.2%} of equity at risk",
            decisions=tuple(self.rule["decisions"]),
        )
        super().__init__(**params)

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def generate(self, df, exchange, symbol, timeframe):
        r = self.rule
        cache: dict = {}
        cond = pd.Series(True, index=df.index)
        for c in r["entry"]:
            cond &= condition_series(df, c, cache)
        stop_s = self._stop_series(df, cache)
        trailing = "|".join(x for x in (
            r["exit"]["trailing"],
            f"time_stop={r['exit']['time_stop_bars']}" if r["exit"]["time_stop_bars"] else None,
        ) if x) or None
        step = TIMEFRAME_MS[timeframe]
        close = df["close"].to_numpy(float)
        out = []
        for i in np.flatnonzero(cond.to_numpy()):
            entry, stop = close[i], float(stop_s.iloc[i])
            if not np.isfinite(stop) or stop >= entry or stop <= 0:
                continue
            target = entry + r["target"]["r"] * (entry - stop) if r["target"] else None
            out.append(Signal(
                ts=int(df["ts"].iloc[i]) + step, strategy=r["name"], exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=float(entry),
                stop=stop, target=target, trailing=trailing, risk_pct=r["risk_pct"],
                reason=self.spec.entry[:200],
            ))
        return out

    def _stop_series(self, df, cache) -> pd.Series:
        st = self.rule["stop"]
        close = df["close"]
        if st["type"] == "atr":
            return close - st["mult"] * atr(df, st["n"])
        if st["type"] == "percent":
            return close * (1 - st["pct"])
        if st["type"] == "lowest_low":
            return df["low"].rolling(st["n"], min_periods=st["n"]).min()
        return _feature(df, parse_expr(st["expr"]), cache)


def _cond_text(c: dict) -> str:
    within = f" (within {c['within']} bars)" if c.get("within", 1) > 1 else ""
    return f"{c['left']} {c['op']} {c['right']}{within}"


def _stop_text(st: dict) -> str:
    return {
        "atr": lambda: f"entry - {st['mult']:g} x ATR({st['n']})",
        "percent": lambda: f"{st['pct']:.1%} below entry",
        "lowest_low": lambda: f"lowest low of the last {st['n']} bars",
        "indicator": lambda: f"{st['expr']} (must be below entry)",
    }[st["type"]]()


def grammar_help() -> str:
    """The rule language described for the model that writes specs."""
    feats = "\n".join(f"- {n}({', '.join(f'{d:g}' for d in dflt)}): {h}" if dflt else f"- {n}: {h}"
                      for n, (dflt, h) in FEATURES.items())
    return f"""Features (arguments optional, defaults shown; add [k] for k bars ago, prefix
"1.5*" to scale):
{feats}
Operators: {', '.join(OPS)}. A condition is {{"left": feature, "op": op, "right": feature or
number, "within": bars (optional, condition true at least once in the last N bars)}}. All entry
conditions must hold on the same bar (AND).
Stop types: {{"type":"atr","mult":2,"n":14}}, {{"type":"percent","pct":0.03}},
{{"type":"lowest_low","n":10}}, {{"type":"indicator","expr":"bb_mid(20,2)"}}.
Target: {{"r": 2}} (reward as a multiple of risk, 0.5-10) or null.
Exit: {{"trailing": "close<ema50" | "close<psar" | "rsi>70" | null, "time_stop_bars": N | null}}.
Timeframes: {', '.join(ALLOWED_TIMEFRAMES)} (up to 3). Long spot entries only; no leverage, no
shorting. risk_pct at most {MAX_RISK_PCT}."""
