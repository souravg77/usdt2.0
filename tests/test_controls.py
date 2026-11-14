"""The Controls page and its store (ops.controls)."""

from aitrader.learn import guards, wallet
from aitrader.market.pairs import configured_symbols
from aitrader.ops import controls


def test_normalize_pair():
    assert controls.normalize_pair("btc") == "BTC/USDT"
    assert controls.normalize_pair("ethusdt") == "ETH/USDT"
    assert controls.normalize_pair(" sol/usdt ") == "SOL/USDT"
    assert controls.normalize_pair("BTC/USDT:USDT") == "BTC/USDT"
    assert controls.normalize_pair("") == "" and controls.normalize_pair("b$d") == ""


def test_blocked_pairs_leave_every_list_but_open_positions_stay(tmp_path):
    env = {"AITRADER_CONTROLS_FILE": str(tmp_path / "c.json"),
           "AITRADER_SPOT_GAINERS_FILE": str(tmp_path / "none1"), "AITRADER_GAINERS_FILE": str(tmp_path / "none2"),
           "AITRADER_SQUEEZE_FILE": str(tmp_path / "none3")}
    pairs = ["BTC/USDT", "ETH/USDT", "PEPE/USDT"]
    assert configured_symbols(pairs, env=env) == pairs
    controls.save(env["AITRADER_CONTROLS_FILE"], {"blocked_pairs": ["pepe", "eth/usdt"]}, by="me")
    assert configured_symbols(pairs, env=env) == ["BTC/USDT"]
    assert configured_symbols(pairs, env=env, keep=["ETH/USDT"]) == ["BTC/USDT", "ETH/USDT"]  # open position
    assert controls.without_blocked(["ETH/USDT:USDT", "SOL/USDT:USDT"], env) == ["SOL/USDT:USDT"]


def test_risk_level_writes_both_engines_and_reads_back(tmp_path):
    state = str(tmp_path / "paper_state.json")
    gpath = guards.path_for(state)
    assert controls.risk_level(guards.load(gpath), wallet.paper_leverage(wallet.market_path(state))) == "custom"
    controls.apply_risk_level("conservative", state, by="me")
    g = guards.load(gpath)
    for m in wallet.MARKETS:
        assert g[m]["risk_pct"] == 0.25 and g[m]["day_pause_pct"] == 2.0 and g[m]["week_close_pct"] == 5.0
    lev = wallet.paper_leverage(wallet.market_path(state))
    assert lev == 1.0 and controls.risk_level(g, lev) == "conservative"
    guards.save(gpath, "spot", {"day_pause_pct": 4.0})
    assert controls.risk_level(guards.load(gpath), lev) == "custom"
    controls.apply_risk_level("aggressive", state)
    assert guards.load(gpath)["futures"]["risk_pct"] == 1.0
    try:
        controls.apply_risk_level("yolo", state)
        raise AssertionError("an unknown level must be refused")
    except ValueError:
        pass


def test_auto_runs_only_when_on_and_due(tmp_path):
    path = str(tmp_path / "c.json")
    ran = []
    runner = lambda cmd: ran.append(cmd[cmd.index("--env-file") + 2:]) or 0  # noqa: E731
    assert controls.run_auto(path, now=lambda: 1e9, runner=runner) == "" and not ran  # off by default
    controls.save(path, {"auto": {"generate": True, "evolve": True, "n": 7, "timeframes": ["1h", "4h"],
                                  "every_hours": 12}}, by="me")
    text = controls.run_auto(path, now=lambda: 1e9, runner=runner)
    assert ran == [["learn", "factory", "--n", "7", "--timeframes", "1h,4h", "--shorts"],
                   ["learn", "evolve", "--n", "7", "--timeframes", "1h,4h", "--shorts"],
                   ["learn", "test"], ["learn", "test", "--market", "futures"]]
    assert "ok" in text and controls.load(path)["auto"]["last_result"] == text
    assert controls.run_auto(path, now=lambda: 1e9 + 3600, runner=runner) == ""  # not due for 12 h
    failing = controls.run_auto(path, now=lambda: 1e9 + 13 * 3600, runner=lambda cmd: 2)
    assert "ended with 2" in failing


def test_auto_settings_are_clamped(tmp_path):
    path = str(tmp_path / "c.json")
    a = controls.save(path, {"auto": {"n": 10_000, "every_hours": 0, "timeframes": ["1m"]}})["auto"]
    assert a["n"] == controls.AUTO_N[1] and a["every_hours"] == 1.0 and a["timeframes"] == list(controls.TIMEFRAMES)


def _web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.chdir(tmp_path)
    paths = Paths(paper_state=str(tmp_path / "learn" / "paper_state.json"), events=str(tmp_path / "ev.jsonl"),
                  controls=str(tmp_path / "controls.json"))
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT", "ETH/USDT"]),
                                   Auth("admin", PW, "k"), JobRunner(str(tmp_path / "jobs"), env_file="x"), paths))
    token = login(client)
    return client, Auth("admin", PW, "k").csrf(token), paths


def test_controls_page_saves_every_control(tmp_path, monkeypatch):
    client, csrf, paths = _web(tmp_path, monkeypatch)
    page = client.get("/controls").text
    for words in ("Mode and emergency stop", "locked", "Risk level: Custom", "Maximum leverage",
                  "Trading timeframes", "Allowed trading pairs", "Automatic strategy generation",
                  'href="/controls"', "Pull the kill switch"):
        assert words in page, words
    assert client.post("/controls/risk-level", data={"csrf": "x", "level": "balanced"}).status_code == 403

    r = client.post("/controls/risk-level", data={"csrf": csrf, "level": "balanced"}, follow_redirects=True)
    assert "Risk level: Balanced" in r.text and "Risk level Balanced applied" in r.text
    gpath = guards.path_for(paths.paper_state)

    client.post("/controls/limits", data={"csrf": csrf, "leverage": "5", "day_spot": "2.5", "day_futures": "4"})
    g = guards.load(gpath)
    assert g["spot"]["day_pause_pct"] == 2.5 and g["futures"]["day_pause_pct"] == 4.0
    assert wallet.paper_leverage(wallet.market_path(paths.paper_state)) == 5.0
    r = client.post("/controls/limits", data={"csrf": csrf, "leverage": "x", "day_spot": "1", "day_futures": "1"},
                    follow_redirects=True)
    assert "Not saved" in r.text

    client.post("/controls/timeframes", data={"csrf": csrf, "tf_spot": ["1h", "4h"], "tf_futures": ["15m"]})
    g = guards.load(gpath)
    assert g["spot"]["timeframes"] == ["1h", "4h"] and g["futures"]["timeframes"] == ["15m"]
    r = client.post("/controls/timeframes", data={"csrf": csrf, "tf_spot": ["1h"]}, follow_redirects=True)
    assert "pick at least one timeframe for Futures AI" in r.text

    client.post("/controls/pairs", data={"csrf": csrf, "listed": "BTC/USDT,ETH/USDT", "allow": ["BTC/USDT"],
                                         "extra": "pepe, WIFUSDT"})
    assert controls.load(paths.controls)["blocked_pairs"] == ["ETH/USDT", "PEPE/USDT", "WIF/USDT"]
    page = client.get("/controls").text
    assert 'value="ETH/USDT"> ETH/USDT' in page and 'value="PEPE/USDT"> PEPE/USDT' in page  # unticked

    r = client.post("/controls/auto", data={"csrf": csrf, "generate": "1", "n": "15", "every_hours": "24",
                                            "timeframes": ["1h"]}, follow_redirects=True)
    a = controls.load(paths.controls)["auto"]
    assert a["generate"] and not a["evolve"] and not a["shorts"] and a["n"] == 15 and a["timeframes"] == ["1h"]
    assert "Automatic generation on: 15 candidates every 24 h" in r.text
