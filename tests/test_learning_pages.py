"""The learning windows: Spot AI and Futures AI each have their own page."""

from fastapi.testclient import TestClient

from aitrader.learn import Entry, Registry


def test_each_engine_has_its_own_learning_window(tmp_path, monkeypatch):
    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.chdir(tmp_path)
    paths = Paths(teacher=str(tmp_path / "t"), events=str(tmp_path / "ev.jsonl"),
                  registry=str(tmp_path / "reg.json"), models=str(tmp_path / "models"))
    reg = Registry(paths.registry)
    reg.add(Entry(id="builtin-ml_boosted", kind="builtin", name="ml_boosted", status="paper",
                  backtest={"out_of_sample": {"trades": 40, "expectancy_r": 0.21}},
                  paper={"markets": {"spot": {"trades": 5, "expectancy_r": 0.5}}}))
    reg.add(Entry(id="short-builtin-ml_boosted", kind="short", name="short: ml_boosted", status="paper",
                  spec={"base_id": "builtin-ml_boosted", "base_kind": "builtin", "base_name": "ml_boosted"},
                  backtest={"out_of_sample": {"trades": 30, "expectancy_r": -0.3}, "failed": ["loses money"]}))
    reg.add(Entry(id="builtin-ema_crossover", kind="builtin", name="ema_crossover", status="paper"))
    reg.save()
    jobs = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x"))
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"),
                     jobs, paths)
    client = TestClient(app)
    assert client.get("/learning/spot", follow_redirects=False).headers["location"] == "/login"
    token = login(client)
    csrf = Auth("admin", PW, "k").csrf(token)

    spot = client.get("/learning/spot").text
    assert "Spot AI learning" in spot and 'href="/learning/futures"' in spot
    assert "builtin-ml_boosted" in spot and "short-builtin-ml_boosted" not in spot  # Spot never shorts
    assert "40 trades · +0.210R" in spot and "5 trades · +0.500R" in spot
    assert "builtin-ema_crossover" not in spot.split("Strategy test")[0]  # only ML ones in the ML table
    assert "AI trade filter: Spot AI" in spot and "Backtest teacher: Spot AI" in spot and "Ollama" in spot

    fut = client.get("/learning/futures").text
    assert "Futures AI learning" in fut and "short-builtin-ml_boosted" in fut and "failed: loses money" in fut
    assert "not tested on the perpetuals yet" in fut  # the shared long has only its spot result
    assert "AI trade filter: Futures AI" in fut and 'href="/teacher?market=futures"' in fut

    r = client.post("/learning/test", data={"csrf": csrf, "market": "futures"}, follow_redirects=False)
    assert r.headers["location"].startswith("/learning/futures?msg=")
    assert submitted[-1] == ([["learn", "test", "--paper", "--market", "futures"]],
                             {"kind": "learn-test", "market": "futures"})
    assert client.post("/learning/test", data={"csrf": "bad", "market": "spot"}).status_code == 403
    # the AI filter's buttons come back to the window they were pressed in
    r = client.post("/trading/ai/train", data={"csrf": csrf, "market": "futures", "back": "/learning/futures"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/learning/futures?msg=")
    r = client.post("/trading/ai/train", data={"csrf": csrf, "market": "spot", "back": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/trading?msg=")
