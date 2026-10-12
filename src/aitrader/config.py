"""Settings read from environment variables (optionally a .env file). Secrets never live in code."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from dotenv import load_dotenv


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


# The exact phrase an operator must set in AITRADER_LIVE_CONFIRM to unlock live orders.
LIVE_CONFIRM_PHRASE = "I accept real-money risk"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# The only exchange the agent uses: Bybit spot (demo trading for fake money, the real account for live).
EXCHANGE = "bybit"

# The 20 most traded coins against USDT (stablecoins left out), Oct 2026; all listed on Bybit spot.
TOP_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'TRX/USDT', 'LINK/USDT', 'AVAX/USDT', 'SUI/USDT', 'LTC/USDT', 'BCH/USDT', 'DOT/USDT', 'TON/USDT', 'NEAR/USDT', 'APT/USDT', 'UNI/USDT', 'AAVE/USDT', 'PEPE/USDT']


@dataclass
class ExchangeKeys:
    api_key: str = ""
    secret: str = ""

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.secret)

    def __repr__(self) -> str:  # never print secrets
        return f"ExchangeKeys(present={self.present})"


@dataclass
class Settings:
    mode: Mode = Mode.PAPER
    live_unlocked: bool = False
    live_confirm: str = ""
    db_path: str = "data/aitrader.db"
    paper_start_usdt: float = 10_000.0
    paper_taker_fee: float = 0.001
    paper_maker_fee: float = 0.001
    paper_slippage_bps: float = 5.0
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframes: list[str] = field(default_factory=lambda: ["1h"])
    keys: dict[str, ExchangeKeys] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file, override=False)
        e = os.environ.get
        keys = {
            "bybit": ExchangeKeys(e("BYBIT_API_KEY", ""), e("BYBIT_API_SECRET", "")),
            "bybit_demo": ExchangeKeys(e("BYBIT_DEMO_API_KEY", ""), e("BYBIT_DEMO_API_SECRET", "")),
        }
        return cls(
            mode=Mode(e("AITRADER_MODE", "paper").strip().lower()),
            live_unlocked=_bool(e("AITRADER_LIVE_UNLOCKED")),
            live_confirm=e("AITRADER_LIVE_CONFIRM", ""),
            db_path=e("AITRADER_DB_PATH", "data/aitrader.db"),
            paper_start_usdt=float(e("AITRADER_PAPER_START_USDT", "10000")),
            paper_taker_fee=float(e("AITRADER_PAPER_TAKER_FEE", "0.001")),
            paper_maker_fee=float(e("AITRADER_PAPER_MAKER_FEE", "0.001")),
            paper_slippage_bps=float(e("AITRADER_PAPER_SLIPPAGE_BPS", "5")),
            symbols=_csv(e("AITRADER_SYMBOLS", ",".join(TOP_PAIRS))),
            timeframes=_csv(e("AITRADER_TIMEFRAMES", "1m,5m,15m,30m,1h,4h,1d")),
            keys=keys,
        )
