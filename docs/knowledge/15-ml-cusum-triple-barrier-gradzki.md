# CUSUM events and triple-barrier labels (Grądzki, Wójcik & Lessmann, 2025)

Source: P. Grądzki, P. Wójcik, S. Lessmann, "Algorithmic crypto trading using information-driven
bars, triple barrier labeling and deep learning", *Financial Innovation* 11:136 (2025), open access
(CC BY 4.0). Uploaded by Shawn on 2026-11-07. Built as the strategy `ml_cusum_barrier`
(`src/aitrader/strategies/ml.py`), with its short twin.

## What the paper tested

- Data: Binance BTCUSDT and ETHUSDT, January 2018 to June 2023, five quarterly out-of-sample test
  periods (Q2 2022 to Q2 2023), 0.1 % fee per side, long and short.
- Five ways to cut the data into bars: time bars (1h, 12h, 24h), volume bars, dollar bars,
  range bars and the **CUSUM filter** (1 %, 2 %, 3 %).
- Two labels: next-bar direction, and the **triple barrier** (take profit, stop loss and a
  time limit; the first one touched is the label). Fixed barriers of 2.5 % or 5 %, 24 bars.
- Seven models: ResNet-LSTM, Attention-LSTM, XGBoost, Transformer encoder, FEDformer,
  Autoformer, TSMixer. A trade is taken when the chance is above 60 % (long) or below 40 % (short).

## What it found

- CUSUM sampling beat time bars; dollar bars were the worst.
- Triple-barrier labels beat next-bar labels. Next-bar models sat near 50 % accuracy and lost
  money after fees (29 of 210 setups profitable).
- ResNet-LSTM was the best model. Transformers did not beat it. XGBoost did worse.
- Best: ETH, CUSUM 2 % with a 5 % triple barrier, +91.6 % a year, Sharpe 1.42, max drawdown
  25 % (buy-and-hold lost 44 % in the same period). BTC: +20.4 % a year, Sharpe 0.51.
- A grid search found CUSUM 2-2.5 % with barriers of 5-6 % best; under 1.5 % or over 3 % worse.
- Volatility-scaled barriers made results worse. The method earned most in volatile months
  (LUNA, mid-2022), lost in the FTX crash, and stalled in the quieter 2023.
- MATIC worked with ETH's settings; LINK needed 5 % / 8 %.

## How it is used here

| Paper | `ml_cusum_barrier` |
|---|---|
| CUSUM filter on 1-minute data | CUSUM filter (simple returns, 2 %) on the 1h and 4h candles; an event is seen at the candle close |
| Triple barrier ±5 %, 24 bars | ±5 %, time barrier 3 days (72 x 1h, 18 x 4h); at the time barrier a rise must beat the 0.15 % round-trip fee |
| Learn and trade on events only | Same: training rows and trade candles are CUSUM events only |
| Features: EMA and SD 5/10/15/20/50, MACD, RSI 6/10/14, stochastic, Williams %R, Bollinger 5, returns, CMF 21, MFI 14, hour and weekday sine/cosine | The same, price-free, plus the CUSUM sums (the paper's advice to put volatility in the inputs) |
| ResNet-LSTM (PyTorch) | Not installed here. Gradient-boosted trees (the paper's XGBoost) and a small scikit-learn neural network vote |
| Long above 60 % | 70 % (Shawn's ML threshold); shorts through the short twin, which learns the upside-down chart |
| Expanding window, retrained each quarter | Retrained every 250 candles on the events of the last 3,000 candles, walk-forward |
| Stop and target | Stop -5 %, target +5 % (1R), time stop 3 days |

## Limits to keep in mind

- The paper's results are a backtest on two coins in 2022-2023. BTC lost money in most of its
  setups. Nothing here promises a profit; the strategy has to pass the same strategy test,
  walk-forward, Monte Carlo and holdout gate as every other one, per engine.
- Its best model is a deep network this platform does not run. The stand-in models are the kind
  the paper found weaker, so judge `ml_cusum_barrier` on its own test results.
- One setting for every coin. The paper notes each coin may need its own; tuning per coin here
  would fit the test to the coins it is judged on.
