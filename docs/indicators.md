# The 20 standard indicators

`indicators/results.py` gives the 20 indicators the master prompt lists one output shape,
`IndicatorResult`:

| Field | Meaning |
|---|---|
| `features` | numbers scaled to compare across coins and timeframes (distance in ATRs or %, oscillators mapped to -1..+1), plus the raw value |
| `direction` | +1 bullish, -1 bearish, 0 neutral or not applicable |
| `confidence` | 0..1, how clear the reading is. It describes the indicator; it is **not** a chance of winning |
| `is_valid` | False during warm-up, when a candle is missing inside the window, or when the value isn't a number. Invalid results always have direction 0 and confidence 0 |
| `warmup_period` | candles needed before the result can be valid |
| `metadata` | zones and thresholds (overbought, squeeze, value area, regime, ...) |

`aitrader indicators --symbol BTC/USDT --timeframe 1h` prints them for the last closed candle of
a stored series (`--json` for all fields).

## How each one decides

| Indicator | Direction | Confidence = 1 at |
|---|---|---|
| EMA (50) | close above a rising EMA +1, below a falling one -1 | 3 ATRs away |
| SMA (200) | same, as the trend baseline | 3 ATRs away |
| VWAP (daily, UTC) | above +1, below -1; ±1 and ±2 sd bands in metadata | 2 sd away |
| RSI (14) | above 55 +1, below 45 -1; 70/30 is only a zone | RSI 100 or 0 |
| MACD (12/26/9) | line and histogram agree on a side | histogram = 0.5 ATR |
| Stochastic RSI | %K above %D +1, below -1; 0.8/0.2 zones | %K-%D gap 0.2 |
| ADX (14) | the stronger DI, only when ADX ≥ 20 | ADX 50 |
| Bollinger (20, 2) | close outside a band (breakout side); squeeze flag | %B at 0 or 1 or beyond |
| ATR (14) | always 0 (volatility only); regime high/normal/low | percentile at 0 or 1 |
| Donchian (20) | close beyond the previous 20 bars' high/low | 1 ATR beyond |
| Ichimoku | side that 2 of 3 agree on: cloud, Tenkan/Kijun, Chikou | all 3 agree |
| Supertrend (10, 3) | its own direction | 2 ATRs from the line |
| OBV | OBV vs its 20-bar EMA (z-score, ±0.5 dead zone) | z = 2 |
| MFI (14) | above 55 +1, below 45 -1; 80/20 zones | MFI 100 or 0 |
| CMF (20) | above +0.05 +1, below -0.05 -1 | ±0.25 |
| Volume profile (100 bars) | above the value area high +1, below the low -1, inside 0 | 1 ATR outside |
| Fibonacci (100 bars) | the swing leg's direction; levels 0.236–0.786 | close on a level |
| Keltner (20, 2) | close outside a band | at the band |
| Williams %R (14) | above -40 +1, below -60 -1; -20/-80 zones | -0 or -100 |
| Funding | contrarian: ≥ 0.05% per 8h -1 (crowded longs), ≤ -0.05% +1 | 0.1% |

An overbought or oversold reading is never a direction on its own: RSI at 75 in a strong trend
is momentum, and the result says +1 with zone "overbought".

## Point in time

Each result uses only the bars in the frame. `closed_only(df, now_ms, step_ms)` drops a candle
that is still forming, so a live decision never reads an unfinished bar. The Ichimoku lagging
span is reported as today's close against the close 26 bars ago (the same comparison without
plotting into the past), and the volume profile and Fibonacci swing use a trailing window.
