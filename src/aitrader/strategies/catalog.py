"""The master prompt's 40 strategies (section 4), each mapped to the class that implements it.

``status`` is one of:
- ``trades``: a long strategy in the registry (its short twin trades the bearish side);
- ``research only``: needs two legs opened together, which the executor doesn't do;
- ``not possible``: can't be built with Bybit as the only exchange.

Every class answers the same interface (``strategies.base``): ``generate``, ``entry_signal``,
``exit_signal``, ``invalidation_condition``, ``calculate_stop_loss``, ``eligible_regimes`` and
``no_trade_conditions``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import families as fam
from . import master as m
from . import quant as q
from .base import Strategy
from .ebook import BollingerSqueeze

CATEGORIES = ("Trend following", "Breakout and volatility", "Mean reversion", "Momentum and price action",
              "Crypto-specific and quantitative")


@dataclass(frozen=True)
class CatalogEntry:
    number: int
    title: str
    cls: type[Strategy]
    status: str = "trades"
    note: str = ""

    @property
    def category(self) -> str:
        return CATEGORIES[(self.number - 1) // 8]

    @property
    def name(self) -> str:
        return self.cls.spec.name


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(1, "EMA crossover", m.EmaCrossover),
    CatalogEntry(2, "SMA crossover", m.SmaCrossover),
    CatalogEntry(3, "MACD trend following", m.MacdTrend),
    CatalogEntry(4, "ADX-filtered trend following", m.AdxTrend),
    CatalogEntry(5, "Supertrend following", fam.SupertrendTrend),
    CatalogEntry(6, "Ichimoku trend confirmation", m.IchimokuTrend),
    CatalogEntry(7, "Multi-timeframe trend alignment", fam.MtfTrendAlignment),
    CatalogEntry(8, "Donchian trend following", m.DonchianTrend),
    CatalogEntry(9, "Donchian channel breakout", m.DonchianBreakout),
    CatalogEntry(10, "Bollinger Band squeeze breakout", BollingerSqueeze),
    CatalogEntry(11, "Keltner Channel breakout", fam.KeltnerBreakout),
    CatalogEntry(12, "Opening-range breakout", m.OpeningRangeBreakout),
    CatalogEntry(13, "Volume-confirmed breakout", m.VolumeBreakout),
    CatalogEntry(14, "ATR expansion breakout", fam.AtrExpansionBreakout),
    CatalogEntry(15, "VWAP reclaim or breakdown", m.VwapReclaim, note="breakdowns are the short twin"),
    CatalogEntry(16, "Support/resistance breakout and retest", m.BreakoutRetest),
    CatalogEntry(17, "RSI reversal", m.RsiReversal),
    CatalogEntry(18, "Stochastic RSI reversal", m.StochRsiReversal),
    CatalogEntry(19, "Bollinger Band mean reversion", m.BollingerMeanReversion),
    CatalogEntry(20, "VWAP mean reversion", m.VwapMeanReversion),
    CatalogEntry(21, "Moving-average deviation reversion", fam.MaDeviationReversion),
    CatalogEntry(22, "Fibonacci retracement reaction", m.FibonacciReaction),
    CatalogEntry(23, "Volume-profile value-area reversion", m.ValueAreaReversion),
    CatalogEntry(24, "Range-bound support/resistance", m.RangeSupportBounce),
    CatalogEntry(25, "MACD histogram momentum", m.MacdHistogramMomentum),
    CatalogEntry(26, "RSI divergence", m.RsiDivergence),
    CatalogEntry(27, "OBV breakout confirmation", fam.ObvBreakout),
    CatalogEntry(28, "MFI momentum confirmation", fam.MfiMomentum),
    CatalogEntry(29, "CMF buying/selling pressure", fam.CmfPressure),
    CatalogEntry(30, "Candlestick reversal patterns", m.CandlestickReversal),
    CatalogEntry(31, "Market structure", m.StructureBreak),
    CatalogEntry(32, "Liquidity-sweep reversal", m.LiquiditySweep),
    CatalogEntry(33, "Funding-rate extreme reversal", fam.FundingExtremeLong,
                 note="crowded longs (funding very positive) is the short twin"),
    CatalogEntry(34, "Funding-rate trend filter", m.FundingTrendFilter),
    CatalogEntry(35, "Spot/perpetual basis trading", q.BasisCarry, "research only",
                 "two legs (spot and perp) must open together"),
    CatalogEntry(36, "Cross-exchange spread arbitrage", q.CrossExchangeArbitrage, "not possible",
                 "needs a second exchange; the platform is Bybit only"),
    CatalogEntry(37, "Pairs trading and cointegration", q.PairsTrading, "research only",
                 "two coins must open together"),
    CatalogEntry(38, "Volatility-regime switching", fam.VolatilityRegimeSwitch),
    CatalogEntry(39, "ML signal classification", m.MlClassifier),
    CatalogEntry(40, "Ensemble voting", fam.EnsembleVote),
)


def by_number(n: int) -> CatalogEntry:
    return CATALOG[n - 1]


def tradeable() -> list[CatalogEntry]:
    return [e for e in CATALOG if e.status == "trades"]
