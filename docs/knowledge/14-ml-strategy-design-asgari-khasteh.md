# Profitable Strategy Design for Trades on Cryptocurrency Markets with Machine Learning Techniques — Mohsen Asgari & Seyed Hossein Khasteh

Source file: "Cryptocurrency machine learning v3" (supplied 2026-10-24), 28 pages, read in full.
Type: research paper (K. N. Toosi University of Technology, around 2021). Market: Binance spot, **4-hour candles**,
three pairs (ETH-USDT, LTC-BTC, ZEC-BTC), mid-2017 to April 2021, long only.
Text quality: clean. The equity and price charts (Figures 1-36) are images and are not in the text; the result
tables (Tables 1-9) are.

## What the paper does
- **Question** (p. 1-2): will the price be higher or lower in the next 4 hours? Treated as a classification
  problem.
- **Models** (p. 3-7): k-Nearest Neighbours, Random Forest and XGBoost (gradient-boosted trees). They also tried
  ridge, logistic regression, SGD, MLP, SVM, Gaussian process, naive Bayes and single decision trees, which made
  "negative profits or lesser profits" (less than half the profit factor).
- **Data** (p. 3-6): OHLCV per 4h candle. Two versions: plain (5 values per candle) and "augmented" with technical
  indicators (19 values per candle). Appendix A (p. 28) lists them: CCI (14 and 30), RSI (14 and 30), DMI (14),
  MACD (12/26), Bollinger bands (20, 2).
- **Inputs** (p. 6): the last 60 candles together, so 60 x 19 = 1,140 numbers (augmented) or 300 (plain).
  Each column scaled by (max - min).
- **Label** (p. 6): 1 when the price 4 hours later is more than about 0.15 % higher (the Binance round-trip fee,
  0.075 % each way), else 0. So a "1" is a move that would have paid its fees.
- **Split** (p. 5-6): the first 95 % to train, the last 5 % (about 66 days) to test. Retraining more often than
  every 66 days is suggested.
- **Settings** (p. 6-7): k-NN with k = 5 / 20 / 100 depending on the pair, distance weighting; Random Forest with
  700-1,000 trees, unlimited depth; XGBoost with eta 0.3, max depth 6, lambda 1, alpha 0.
- **Strategy** (p. 7): every 4 hours, if the model predicts a rise above the fee, buy (or keep holding); when it
  stops predicting that, sell. Fixed position size. No stop loss.
- **Results** (p. 12-23, Tables 1-9): accuracy 0.47-0.59; profit factor 1.12-1.63 on the 66-day test; 43-291 trades
  per test; win rates 48-57 %. Best: k-NN on ZEC-BTC, profit factor 1.63 (1.60 in the abstract).
- **Discussion** (p. 24-25): indicators helped a lot ("from a negative return to a positive one"); the labelling
  threshold and hyperparameters changed results a lot and "there may be a need to reconfigure them from time to
  time"; models behave differently per market; equity curves "lack smoothness", so large risks remain; liquidity
  was not modelled. Future work: fundamentals, **ensembling the models**, social data, deep learning,
  reinforcement learning, ML for risk management.

## What to be careful about (our reading)
- One 66-day test per pair is a small sample; 43 trades (ZEC-BTC k-NN) cannot separate skill from luck.
- k and the number of trees were chosen per pair, and the paper does not say on which data. If on the test
  period, the results are optimistic.
- Scaling each column by its max and min over the whole history lets the test period's prices into training.
- No stop loss and no slippage; fees only through the label threshold.
- Accuracy close to 50 % is normal for this problem; the profit comes from how big wins are against losses.

## What the agent does with it
| Paper idea | Where it is in the code |
|---|---|
| k-NN + Random Forest + gradient boosting, voting together (the paper's future work #2) | `ml_ensemble` in `src/aitrader/strategies/ml.py` |
| Last 60 candles x 19 values (OHLCV, CCI 14/30, RSI 14/30, DMI 14, MACD, Bollinger) | `paper_rows` and `MlEnsemble._features` |
| Label = rise above the 0.15 % round-trip fee within 4 hours (1 candle on 4h, 4 on 1h) | `MlEnsemble._labels` |
| Hold while the models expect a rise, sell when they stop | its exit rule `ml_ensemble_down:...` |
| Retrain often | every 250 candles, on up to the last 1,500 |
| Indicators help | CCI 14/30 and RSI 30 added to `ml_boosted`'s features too (25 now) |

Changed on purpose: values are price-free ratios of each candle (no whole-history scaling), one setting for every
pair (no per-pair tuning), a 2 x ATR safety stop plus a 4R ceiling and a time stop, 60 trees and 40 boosting
rounds (seconds per retrain on the server). It is an untested candidate like every strategy: Spot AI tests it on
spot candles and Futures AI on the perpetuals (with a short twin), and only a pass of the full gate
(out-of-sample, walk-forward, Monte Carlo, holdout) keeps it trading. Nothing here promises a profit.
