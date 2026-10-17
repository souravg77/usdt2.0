"""Phase 2: mark and index price, basis, funding and liquidation for Bybit USDT perpetuals.
Fake clients and sockets stand in for Bybit."""

import json

import pytest

from aitrader.data import derivs
from aitrader.data.bybit_ws import PUBLIC_LINEAR_URL, BybitLinearStream, BybitSpotStream
from aitrader.trade import executor as executor_mod

from test_trade import KEY, PERP, run
from test_trade import ex, px  # noqa: F401  (fixtures)

LINEAR = {"symbol": "BTCUSDT", "lastPrice": "101.0", "markPrice": "100.5", "indexPrice": "100.0",
          "fundingRate": "0.0001", "nextFundingTime": "1760004000000", "fundingIntervalHour": "8",
          "openInterest": "12345.6"}


# ---- readings -----------------------------------------------------------------------------------


def test_snapshot_basis_premium_and_funding():
    s = derivs.parse_ticker(LINEAR, "BTC/USDT")
    assert (s.last, s.mark, s.index) == (101.0, 100.5, 100.0)
    assert s.basis == pytest.approx(0.01) and s.mark_premium == pytest.approx(0.005)
    assert s.funding_annualized == pytest.approx(0.0001 * 3 * 365)
    assert s.funding_in_s(1760004000000 - 90_000) == 90.0
    assert s.open_interest == 12345.6 and s.as_dict()["basis"] == pytest.approx(0.01)
    # a delta only carries what changed; the rest is kept
    s = derivs.parse_ticker({"markPrice": "99.0", "indexPrice": ""}, "BTC/USDT", s)
    assert s.mark == 99.0 and s.index == 100.0 and s.last == 101.0
    assert derivs.DerivSnapshot("X").basis is None and derivs.DerivSnapshot("X").funding_annualized is None


def test_rest_snapshot_reads_the_perpetual():
    class Client:
        async def fetch_ticker(self, symbol):
            self.asked = symbol
            return {"last": 101.0, "timestamp": 7, "info": dict(LINEAR, lastPrice="")}

    c = Client()
    s = run(derivs.fetch_snapshot(c, "BTC/USDT"))
    assert c.asked == PERP and s.last == 101.0 and s.mark == 100.5 and s.ts == 7


def test_liquidation_distance_and_stop_rule():
    assert derivs.liquidation_distance(100.0, 130.0) == pytest.approx(0.30)
    assert derivs.liquidation_distance(100.0, 70.0, short=False) == pytest.approx(0.30)
    assert derivs.liquidation_distance(100.0, None) is None
    assert derivs.stop_inside_liquidation(100.0, 110.0, 130.0)  # a third of the way
    assert not derivs.stop_inside_liquidation(100.0, 120.0, 130.0)  # two thirds
    assert derivs.stop_inside_liquidation(100.0, 120.0, None)  # unknown: the entry check stands
    assert derivs.stop_inside_liquidation(100.0, 90.0, 70.0, short=False)


def test_linear_stream_tracks_mark_index_and_funding():
    s = BybitLinearStream(["BTC/USDT"], clock=lambda: 5.0)
    assert s.url == PUBLIC_LINEAR_URL and s.category == "linear"
    assert s._handle(json.dumps({"topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 9, "data": LINEAR}))
    assert s.mark_price("BTC/USDT") == 100.5 and s.index_price("BTC/USDT") == 100.0
    assert s._handle(json.dumps({"topic": "tickers.BTCUSDT", "type": "delta", "ts": 10,
                                 "data": {"symbol": "BTCUSDT", "markPrice": "102"}}))
    d = s.derivs("BTC/USDT")
    assert d.mark == 102.0 and d.last == 101.0 and d.funding_rate == 0.0001 and d.ts == 10
    assert d.mark_premium == pytest.approx(0.02)
    with pytest.raises(ValueError):
        BybitSpotStream(["BTC/USDT"], category="option")


# ---- adapter ------------------------------------------------------------------------------------


class Client:
    markets = {PERP: {"id": "BTCUSDT"}}

    def __init__(self, idx="0"):
        self.idx, self.log_args = idx, None

    def market(self, symbol):
        return self.markets[symbol]

    async def private_get_v5_account_info(self):
        return {"result": {"marginMode": "ISOLATED_MARGIN"}}

    async def set_leverage(self, lev, symbol):
        pass

    async def fetch_positions(self, symbols):
        return [{"symbol": PERP, "side": "short", "contracts": 0.5, "entryPrice": 100.0, "markPrice": 101.0,
                 "liquidationPrice": None, "leverage": 2, "marginMode": "isolated",
                 "info": {"positionIdx": self.idx, "liqPrice": "149.5"}}]

    async def private_get_v5_account_transaction_log(self, params):
        self.log_args = params
        return {"result": {"list": [{"funding": "0.12", "change": "-0.12"}, {"change": "0.05"}]}}


def _adapter(client):
    from aitrader.config import LIVE_CONFIRM_PHRASE, Mode, Settings
    from aitrader.exchanges import BybitAdapter

    return BybitAdapter(Settings(mode=Mode.LIVE, live_unlocked=True, live_confirm=LIVE_CONFIRM_PHRASE),
                        client=client)


def test_adapter_reads_the_real_liquidation_price_and_funding():
    c = Client()
    ad = _adapter(c)
    info = run(ad.perp_position_info(PERP))
    assert info == {"size": 0.5, "entry": 100.0, "mark": 101.0, "liq_price": 149.5, "leverage": 2.0,
                    "margin_mode": "isolated", "position_idx": 0}
    assert run(ad.funding_paid(PERP, 1)) == pytest.approx(0.12 - 0.05)
    assert c.log_args["type"] == "SETTLEMENT" and c.log_args["symbol"] == "BTCUSDT"
    assert c.log_args["startTime"] > 1  # Bybit answers 7 days at most


def test_adapter_refuses_hedge_mode():
    from aitrader.exchanges import PerpNotReady

    with pytest.raises(PerpNotReady, match="One-Way"):
        run(_adapter(Client(idx="2")).prepare_perp(PERP, 2.0))


# ---- executor -----------------------------------------------------------------------------------


def halts(events):
    return [x for x in events.read() if x["kind"] == "halt"]


def test_short_close_books_funding(px):
    make, adapter, paper, events = px
    paid = []

    async def funding_paid(symbol, since_ms):
        paid.append((symbol, since_ms))
        return 0.25

    adapter.funding_paid = funding_paid
    e = make()
    run(e.tick())
    adapter.ask = 106.0
    run(e.tick())
    row = e.state["closed"][0]
    assert row["funding_usdt"] == 0.25 and paid[0][0] == PERP
    gross = (row["entry"] - row["exit"]) * row["amount"]
    assert row["pnl_usdt"] < gross - 0.25  # fees and funding both come off


def test_short_near_liquidation_halts_once(px):
    make, adapter, paper, events = px
    info = {"mark": 100.0, "liq_price": 140.0}

    async def perp_position_info(symbol, side="short"):
        return dict(info)

    adapter.perp_position_info = perp_position_info
    clock = [executor_mod.LIQ_CHECK_S * 1000.0]
    e = make()
    e.clock = lambda: clock[0]
    run(e.tick())
    assert not halts(events) and e.state["positions"][KEY]["liq_price"] == 140.0
    info.update(mark=137.0)  # 2.2% from liquidation (the stop at 105 is far behind: price data only)
    clock[0] += executor_mod.LIQ_CHECK_S
    run(e.tick())
    assert e.control.halted and "from liquidation" in e.control.state()["reason"]
    clock[0] += executor_mod.LIQ_CHECK_S
    run(e.tick())
    assert len(halts(events)) == 1


def test_stop_beyond_half_the_real_liquidation_room_halts(px):
    make, adapter, *_ = px

    async def perp_position_info(symbol, side="short"):
        return {"mark": 100.0, "liq_price": 108.0}  # Bybit's liquidation is nearer than estimated

    adapter.perp_position_info = perp_position_info
    e = make()
    run(e.tick())
    assert e.control.halted and "halfway" in e.control.state()["reason"]


def test_cli_prints_live_perpetual_readings(monkeypatch, capsys):
    from aitrader import cli
    from aitrader.data import context

    class Fake:
        async def fetch_ticker(self, symbol):
            if symbol.startswith("ETH"):
                raise RuntimeError("no market")
            return {"last": 101.0, "timestamp": 1, "info": LINEAR}

        async def close(self):
            self.closed = True

    fake = Fake()
    monkeypatch.setattr(context, "make_client", lambda: fake)
    assert run(cli._print_derivs(["BTC/USDT", "ETH/USDT"])) == 0
    out = capsys.readouterr().out
    assert "+1.000%" in out and "+0.0100%" in out and "ETH/USDT   unavailable" in out and fake.closed
