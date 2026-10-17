"""Fair value gaps, order blocks and liquidity sweeps: the zones, the rule fields, the strategies and the chart."""

import numpy as np
import pandas as pd

from aitrader.indicators import fair_value_gaps, liquidity_sweeps, order_blocks, retest
from aitrader.learn.rules import RuleStrategy, validate_spec
from aitrader.strategies import STRATEGIES

H = 3_600_000


def bars(rows):
    return pd.DataFrame([{"ts": i * H, "open": o, "high": h, "low": lo, "close": c, "volume": 10.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def test_a_bullish_gap_stays_open_until_price_fills_it():
    df = bars([(10, 11, 9, 10.5), (10.5, 13, 10.4, 12.8), (12.8, 14, 12, 13.5),  # low 12 > high 11: gap 11-12
               (13.5, 14, 12.5, 13), (13, 13.2, 11.5, 12.6),  # dips into the gap, closes above 11
               (12.6, 12.7, 10.8, 11)])  # low 10.8 fills it
    g = fair_value_gaps(df)
    assert list(g["bull_new"]) == [0, 0, 1, 0, 0, 0]
    assert g["bull_top"][2] == 12 and g["bull_bottom"][2] == 11 and g["bull_top"][4] == 12
    assert np.isnan(g["bull_top"][5])  # filled
    hit = retest(df, g["bull_top"], g["bull_bottom"])
    assert list(hit) == [0, 0, 0, 0, 1, 0]
    assert fair_value_gaps(df, max_age=1)["bull_top"].isna()[4]  # too old by then


def test_a_bearish_gap_mirrors_it():
    df = bars([(14, 15, 13, 13.5), (13.5, 13.6, 11, 11.2), (11.2, 12, 10, 10.5)])  # high 12 < low 13
    g = fair_value_gaps(df)
    assert g["bear_new"][2] == 1 and g["bear_top"][2] == 13 and g["bear_bottom"][2] == 12


def test_an_order_block_is_the_last_down_candle_before_a_break():
    flat = [(10, 10.5, 9.5, 10)] * 10
    df = bars(flat + [(10.2, 10.3, 9.6, 9.8),  # down candle: the block 9.6-10.3
                      (9.8, 11.5, 9.7, 11.2),  # closes above the 10-bar high (10.5)
                      (11.2, 11.3, 10.1, 10.6),  # dips into the block, closes above 9.6
                      (10.6, 10.7, 9.0, 9.2)])  # closes below its low: no longer valid
    ob = order_blocks(df)
    assert ob["bull_new"][11] == 1 and ob["bull_bottom"][11] == 9.6 and ob["bull_top"][11] == 10.3
    assert retest(df, ob["bull_top"], ob["bull_bottom"])[12] == 1
    assert np.isnan(ob["bull_top"][13])


def test_a_sweep_takes_the_lows_and_closes_back_above():
    df = bars([(10, 10.5, 9.5, 10)] * 20 + [(10, 10.2, 9.2, 9.9), (9.9, 10, 9.0, 9.1)])
    sw = liquidity_sweeps(df)
    assert sw["low"][20] == 1 and sw["low"][21] == 0 and sw["high"].sum() == 0


def test_rules_can_use_the_zones():
    spec = validate_spec({
        "name": "gap dip", "timeframes": ["1h"], "source": {"book": "t"},
        "entry": [{"left": "in_bull_fvg(50)", "op": ">", "right": 0},
                  {"left": "close", "op": ">", "right": "fvg_bull_bottom(50)[1]"}],
        "stop": {"type": "percent", "pct": 0.05}, "target": {"r": 2}})
    df = bars([(10, 11, 9, 10.5), (10.5, 13, 10.4, 12.8), (12.8, 14, 12, 13.5),
               (13.5, 14, 12.5, 13), (13, 13.2, 11.5, 12.6), (12.6, 12.7, 12.1, 12.5)])
    sigs = RuleStrategy(spec).generate(df, "bybit", "BTC/USDT", "1h")
    assert [s.ts for s in sigs] == [5 * H]  # the close of bar 4, the dip into the gap
    for name in ("fvg_bull", "fvg_bear", "in_bear_fvg", "bull_ob_top", "in_bull_ob", "in_bear_ob",
                 "sweep_low", "sweep_high"):
        validate_spec({**spec, "entry": [{"left": name, "op": ">=", "right": 0}]})


def test_the_three_strategies_are_built_in_and_run():
    from test_strategies import synthetic

    df = synthetic(n=1500)
    for name in ("fvg_retest", "order_block_retest", "liquidity_sweep_reversal", "sweep_then_gap"):
        strat = STRATEGIES[name]()
        sigs = strat.generate(df, "bybit", "BTC/USDT", "1h")
        assert all(s.stop < s.entry < s.target for s in sigs), name


def test_the_chart_draws_the_zones_and_the_strategies_count_them():
    from aitrader.learn import Entry
    from aitrader.learn.keep import indicators
    from aitrader.web import studies

    rows = [[i * H, 10, 11, 9, 10.5, 1] for i in range(3)] + [[3 * H, 12.8, 14, 12, 13.5, 1]]
    out = studies.compute(rows)
    assert {"fvg_bull_top", "ob_bull_bottom", "sweep"} <= set(out["overlays"])
    for name in ("fvg_retest", "order_block_retest", "liquidity_sweep_reversal"):
        assert "smc" in indicators(Entry(id=f"builtin-{name}", kind="builtin", name=name))
    rule = Entry(id="r", kind="rule", name="r", spec={"entry": [{"left": "sweep_low(20)", "op": ">", "right": 0}]})
    assert "smc" in indicators(rule)


def test_sweep_then_gap_needs_both_the_sweep_and_the_gap():
    from aitrader.strategies.smc import SweepThenGap

    flat = [(10, 10.5, 9.5, 10)] * 200
    rows = flat + [(10, 10.2, 9.2, 10.1),  # sweeps the 15-bar low (9.5), closes back above
                   (10.1, 11.5, 10.0, 11.4),  # the fast move up
                   (11.4, 12.0, 10.4, 11.8)]  # low 10.4 > sweep candle high 10.2: the gap
    s = SweepThenGap()
    s.params["atr_pct_max"] = 1.0  # only the setup is under test, not the calm-market filter
    sigs = s.generate(bars(rows), "bybit", "BTC/USDT", "1h")
    assert [x.ts for x in sigs] == [203 * H] and sigs[0].stop < 9.2 and sigs[0].stop < sigs[0].entry < sigs[0].target
    no_gap = rows[:-1] + [(11.4, 12.0, 10.1, 11.8)]  # low 10.1 is below the sweep candle high: no gap
    assert s.generate(bars(no_gap), "bybit", "BTC/USDT", "1h") == []
    assert "sweep_then_gap" in STRATEGIES
