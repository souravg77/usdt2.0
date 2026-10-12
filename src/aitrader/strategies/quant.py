"""The master prompt's two-leg strategies (#35 basis, #37 pairs) and #36, which can't be built here.

Both need two positions opened and closed together (spot and perpetual, or two coins). The
executor trades one leg at a time with its own stop, and a half-filled pair is a naked position,
so these are **research only**: they measure the opportunity after every cost and never produce a
trade signal (``generate`` returns nothing, and they are not in the strategy registry). They exist
so the numbers can be studied before anyone builds a two-leg executor.

#36 cross-exchange spread arbitrage needs a second exchange; the platform trades Bybit only, so it
is recorded as not possible here rather than simulated.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .base import Signal, Strategy, StrategySpec

SOURCE = "Master prompt section 4 (research only: two legs)"
NO_SIGNALS = "Research only: never produces a trade signal (two legs must open and close together)."

# MacKinnon (2010) critical values for the Engle-Granger cointegration test, 2 variables with a
# constant: the residual ADF t-statistic must be below these.
EG_CRITICAL = {0.01: -3.90, 0.05: -3.34, 0.10: -3.04}


class _ResearchOnly(Strategy):
    eligible_regimes = ()  # never opens a trade

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def generate(self, df, exchange, symbol, timeframe) -> list[Signal]:
        return []

    def no_trade_conditions(self, df, timeframe) -> list[str]:
        return [NO_SIGNALS]


# ---- #35 spot / perpetual basis (cash and carry) ------------------------------------------------


class BasisCarry(_ResearchOnly):
    spec = StrategySpec(
        name="basis_carry",
        source=SOURCE + ": #35 Spot/perpetual basis trading (cash and carry)",
        timeframes=("1h", "4h"),
        regime="Positive funding: longs pay shorts.",
        setup="Buy spot and sell the same amount of the USDT perpetual (delta neutral). The "
        "position earns the funding the perpetual's longs pay, plus any basis that closes.",
        entry="When the expected funding over the holding period (the average of the last 21 "
        "funding payments x payments held) beats every cost: spot and perp fees on the way in and "
        "out, slippage on all four fills, and the basis paid at entry.",
        stop="None in the usual sense: the risk is funding turning negative, the basis widening "
        "and liquidation of the short leg; research only.",
        target="Hold for `hold_hours` (default 7 days), then close both legs.",
        filter=NO_SIGNALS,
        size="Not traded.",
        decisions=("Needs a two-leg executor before it could trade; the costs are what decides.",),
    )

    @classmethod
    def default_params(cls) -> dict:
        from ..backtest.engine import BacktestConfig  # the engine imports strategies

        cfg = BacktestConfig()
        return {"hold_hours": 168.0, "funding_hours": 8.0, "avg_payments": 21, "spot_fee": cfg.taker_fee,
                "perp_fee": cfg.perp_taker_fee, "slippage_bps": cfg.slippage_bps}

    def costs(self) -> float:
        """Round-trip cost as a fraction of one leg's notional: 4 fills (spot in/out, perp in/out)."""
        p = self.params
        return 2 * p["spot_fee"] + 2 * p["perp_fee"] + 4 * p["slippage_bps"] / 10_000

    def evaluate(self, funding_rate: float, basis: float = 0.0, hold_hours: float | None = None) -> dict:
        """Expected carry for one funding rate (per payment) and an entry basis ((perp - spot) /
        spot; buying spot and selling the perp *above* it adds that gap if it closes)."""
        p = self.params
        hours = p["hold_hours"] if hold_hours is None else hold_hours
        payments = hours / p["funding_hours"]
        gross = funding_rate * payments + basis
        net = gross - self.costs()
        return {"funding_rate": funding_rate, "payments": payments, "gross": gross, "costs": self.costs(),
                "net": net, "net_annualized": net * (8760 / hours) if hours else math.nan, "worth_it": net > 0}

    def opportunities(self, df: pd.DataFrame) -> pd.DataFrame:
        """For each bar with a known funding rate: the expected carry using the average of the last
        ``avg_payments`` distinct funding values (only past values)."""
        if "funding_rate" not in df:
            return pd.DataFrame(columns=["funding_avg", "net", "worth_it"])
        f = df["funding_rate"].astype(float)
        changed = f.ne(f.shift())
        distinct = f.where(changed)
        avg = distinct.dropna().rolling(self.params["avg_payments"], min_periods=3).mean().reindex(df.index).ffill()
        rows = [self.evaluate(a) if math.isfinite(a) else {"net": math.nan, "worth_it": False} for a in avg.fillna(np.nan)]
        out = pd.DataFrame(rows, index=df.index)
        out.insert(0, "funding_avg", avg)
        return out[["funding_avg", "net", "worth_it"]]


# ---- #36 cross-exchange arbitrage ---------------------------------------------------------------


class CrossExchangeArbitrage(_ResearchOnly):
    spec = StrategySpec(
        name="cross_exchange_arbitrage",
        source="Master prompt section 4: #36 Cross-exchange spread arbitrage (not possible on Bybit only)",
        timeframes=(),
        regime="",
        setup="Buy on the cheaper exchange and sell on the dearer one.",
        entry="Not built: the platform trades on Bybit only, so there is no second venue.",
        stop="-", target="-", filter="Not possible with one exchange.", size="Not traded.",
        decisions=("Left out on the owner's decision to stay on Bybit only.",),
    )

    def no_trade_conditions(self, df, timeframe) -> list[str]:
        return ["not possible on Bybit only: needs a second exchange"]


# ---- #37 pairs trading --------------------------------------------------------------------------


def _ols(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Coefficients and their standard errors."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(1, len(y) - X.shape[1])
    sigma2 = resid @ resid / dof
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    return beta, np.sqrt(np.clip(np.diag(cov), 0, None))


def adf_tstat(series: np.ndarray, lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic (with a constant) of ``series``: very negative means
    it keeps returning to its mean."""
    x = np.asarray(series, dtype=float)
    dx = np.diff(x)
    rows = len(dx) - lags
    if rows < 10:
        return math.nan
    cols = [x[lags:-1], np.ones(rows)] + [dx[lags - k:-k] for k in range(1, lags + 1)]
    beta, se = _ols(dx[lags:], np.column_stack(cols))
    return float(beta[0] / se[0]) if se[0] > 0 else math.nan


def engle_granger(y: pd.Series, x: pd.Series, lags: int = 1) -> dict:
    """Engle-Granger two-step test on log prices: regress y on x, then ADF on the residual."""
    ly, lx = np.log(y.to_numpy(float)), np.log(x.to_numpy(float))
    ok = np.isfinite(ly) & np.isfinite(lx)
    ly, lx = ly[ok], lx[ok]
    if len(ly) < 50:
        return {"beta": math.nan, "alpha": math.nan, "adf_t": math.nan, "cointegrated": False, "level": None}
    (alpha, beta), _ = _ols(ly, np.column_stack([np.ones(len(lx)), lx]))
    t = adf_tstat(ly - alpha - beta * lx, lags)
    level = next((lv for lv, crit in sorted(EG_CRITICAL.items()) if t < crit), None)
    return {"beta": float(beta), "alpha": float(alpha), "adf_t": t, "cointegrated": level is not None and level <= 0.05,
            "level": level}


def ou_half_life(spread: pd.Series) -> float:
    """Half-life in bars of an Ornstein-Uhlenbeck fit (dS = theta (mu - S) dt): how long a gap
    from the mean takes to halve. Infinite when the spread doesn't mean-revert."""
    s = spread.dropna().to_numpy(float)
    if len(s) < 20:
        return math.inf
    (_c, b), _ = _ols(np.diff(s), np.column_stack([np.ones(len(s) - 1), s[:-1]]))
    return float(-math.log(2) / b) if b < -1e-9 else math.inf


class PairsTrading(_ResearchOnly):
    spec = StrategySpec(
        name="pairs_trading",
        source=SOURCE + ": #37 Pairs trading and cointegration",
        timeframes=("1h", "4h"),
        regime="Two coins whose prices move together (cointegrated at 5%).",
        setup="Fit log(A) = alpha + beta x log(B) on the formation window (default 500 bars), test "
        "the residual with the Engle-Granger ADF test, and estimate the Ornstein-Uhlenbeck "
        "half-life. Trade only if cointegrated and the half-life is 2-100 bars.",
        entry="Spread z-score (rolling mean and sd over the half-life x 3) below -2: buy A, sell "
        "beta x B. Above +2: the reverse.",
        stop="|z| above 4, or the pair stops being cointegrated on re-test.",
        target="z back inside ±0.5; time stop at 3 half-lives.",
        filter=NO_SIGNALS + " Costs: 4 fills (both legs in and out) at perp fees plus slippage.",
        size="Not traded.",
        decisions=("Engle-Granger and OU are written here (no statistics library needed).",),
    )

    @classmethod
    def default_params(cls) -> dict:
        from ..backtest.engine import BacktestConfig

        cfg = BacktestConfig()
        return {"formation": 500, "entry_z": 2.0, "exit_z": 0.5, "stop_z": 4.0, "min_half_life": 2.0,
                "max_half_life": 100.0, "fee": cfg.perp_taker_fee, "slippage_bps": cfg.slippage_bps}

    def analyse(self, a: pd.DataFrame, b: pd.DataFrame) -> dict:
        """Cointegration, half-life and the z-score signals of A against B on aligned closes. The
        hedge ratio comes from the formation window only; z-scores after it are out of sample."""
        p = self.params
        both = pd.concat([a.set_index("ts")["close"], b.set_index("ts")["close"]], axis=1, join="inner").dropna()
        both.columns = ["a", "b"]
        form = both.iloc[:p["formation"]]
        eg = engle_granger(form["a"], form["b"])
        out = {**eg, "half_life": math.inf, "tradeable": False, "trades": [], "cost_per_trade": 4 * (p["fee"] + p["slippage_bps"] / 10_000)}
        if not math.isfinite(eg["beta"]):
            return out
        spread = np.log(both["a"]) - eg["alpha"] - eg["beta"] * np.log(both["b"])
        hl = ou_half_life(spread.iloc[:p["formation"]])
        out["half_life"] = hl
        out["tradeable"] = bool(eg["cointegrated"] and p["min_half_life"] <= hl <= p["max_half_life"])
        if not out["tradeable"]:
            return out
        win = max(10, int(round(3 * hl)))
        z = (spread - spread.rolling(win).mean()) / spread.rolling(win).std()
        pos, opened, trades = 0, None, []
        for i in range(p["formation"], len(z)):
            zi = z.iloc[i]
            if not math.isfinite(zi):
                continue
            if pos == 0 and abs(zi) >= p["entry_z"]:
                pos, opened = (1 if zi < 0 else -1), i
            elif pos and (abs(zi) <= p["exit_z"] or abs(zi) >= p["stop_z"] or i - opened >= 3 * hl):
                gross = pos * (spread.iloc[i] - spread.iloc[opened])
                trades.append({"open_ts": int(both.index[opened]), "close_ts": int(both.index[i]), "side": pos,
                               "gross": float(gross), "net": float(gross - out["cost_per_trade"]),
                               "why": "mean" if abs(zi) <= p["exit_z"] else "stop" if abs(zi) >= p["stop_z"] else "time"})
                pos = 0
        out["trades"] = trades
        out["z"] = z
        return out


RESEARCH_ONLY = (BasisCarry, CrossExchangeArbitrage, PairsTrading)
