"""Phase 7: the seven-step promotion ladder, and LIVE ENABLED only through a typed confirmation
that nothing can skip (no flag, no piped input, no hand-written or edited approval file)."""

import json

import pytest

from aitrader import cli
from aitrader.learn import Registry
from aitrader.learn.ladder import LADDER, Evidence, label, ladder_state
from aitrader.learn.registry import STATUSES, Entry
from aitrader.trade.golive import _seal, approval_intact, confirmation_phrase

from test_trade import _beats, _gate_world

PROMPT_NAMES = ("RESEARCH", "BACKTESTED", "OOS_VALIDATED", "PAPER_DEMO", "SHADOW_MONITORED",
                "LIVE_ELIGIBLE", "LIVE_ENABLED")


def _ready(tmp_path):
    """A strategy with a passed backtest and 120 paper trades, the gate's manual items done."""
    gl, events, _reg = _gate_world(tmp_path)
    _beats(events)
    gl.attest("keys", by="Shawn")
    gl.attest("kill_switch", by="Shawn")
    return gl


def _state(tmp_path, gl, eid="s1"):
    ev = Evidence(gl.root, str(tmp_path / "trade"), gl.registry_path, gl.models_dir, gl.paper_state)
    return ladder_state(Registry(gl.registry_path).get(eid), ev)[0]


def test_the_ladder_has_the_seven_rungs_in_order():
    assert len(LADDER) == 7
    ours = [label(s).split(" (")[0].replace("OUT-OF-SAMPLE", "OOS").replace("-", "_").replace(" ", "_")
            for s in LADDER]
    assert ours == ["RESEARCH", "BACKTESTED", "OOS_VALIDATED", "PAPER", "SHADOW_MONITORED", "LIVE_ELIGIBLE",
                    "LIVE_ENABLED"]
    assert [p.split("_")[0] for p in PROMPT_NAMES] == [o.split("_")[0] for o in ours]
    # the registry stores only the lower rungs; the top three come from evidence
    assert set(LADDER[:4]) <= set(STATUSES) and not set(LADDER[4:]) & set(STATUSES)
    e = Entry(id="a", kind="rule", name="a")
    for rung in LADDER[4:]:
        with pytest.raises(ValueError):
            e.set_status(rung, "skip the ladder")


def test_one_strategy_climbs_every_rung_in_turn(tmp_path):
    gl = _ready(tmp_path)
    reg = Registry(gl.registry_path)
    e = reg.get("s1")
    for rung in LADDER[:3]:
        e.set_status(rung, "test")
        reg.save()
        assert _state(tmp_path, gl) == rung
    e.set_status("paper", "test")
    reg.save()
    with open(gl.paper_state, "w") as fh:  # too few paper trades for shadow
        json.dump({"trades": [{"entry_id": "s1", "r": 0.3, "exit_ts": 1}] * 29}, fh)
    assert _state(tmp_path, gl) == "paper"
    _gate_world(tmp_path)  # 120 paper trades again
    assert _state(tmp_path, gl) == "shadow"
    assert gl.evaluate(["s1"])["passed"]
    assert _state(tmp_path, gl) == "live_eligible"
    gl.approve(["s1"], by="Shawn", confirmation=confirmation_phrase(["s1"]))
    assert _state(tmp_path, gl) == "live_enabled"
    gl.revoke(by="Shawn")
    assert _state(tmp_path, gl) == "live_eligible"


def test_approval_needs_the_gate_and_the_exact_phrase(tmp_path):
    gl, _events, _reg = _gate_world(tmp_path)  # gate not passed: no stability, no attestations
    with pytest.raises(ValueError, match="gate does not pass"):
        gl.approve(["s1"], by="Shawn", confirmation=confirmation_phrase(["s1"]))
    gl = _ready(tmp_path)
    for typed in ("", "yes", "I approve s1", confirmation_phrase(["s2"]), confirmation_phrase(["s1"]).upper()):
        with pytest.raises(ValueError, match="not confirmed"):
            gl.approve(["s1"], by="Shawn", confirmation=typed)
    with pytest.raises(TypeError):
        gl.approve(["s1"], by="Shawn")  # there is no default confirmation
    assert gl.guard("s1") == ["no go-live approval (aitrader golive approve)"]
    a = gl.approve(["s1"], by="Shawn", confirmation=confirmation_phrase(["s1"]))
    assert approval_intact(a) and a["confirmation"] == "I approve s1 for real-money trading"
    assert gl.guard("s1") == []


def test_a_hand_written_or_edited_approval_counts_for_nothing(tmp_path):
    gl = _ready(tmp_path)
    gl.evaluate(["s1"])
    forged = {"by": "someone", "at": 0, "report": {"passed": True}, **gl._fingerprint(["s1"])}
    for bad in (forged, {**forged, "confirmation": confirmation_phrase(["s1"])},
                {**forged, "confirmation": confirmation_phrase(["s1"]), "seal": "0" * 64}):
        gl._save("approval.json", bad)
        assert not approval_intact(bad)
        assert "edited" in gl.guard("s1")[0]
        assert _state(tmp_path, gl) == "live_eligible"
    good = gl.approve(["s1"], by="Shawn", confirmation=confirmation_phrase(["s1"]))
    assert _state(tmp_path, gl) == "live_enabled"
    for edit in ({"by": "someone else"}, {"report": {"passed": False}},
                 {"strategies": {**good["strategies"], "s2": "x"}}):
        gl._save("approval.json", {**good, **edit})
        assert gl.guard("s1") and _state(tmp_path, gl) != "live_enabled"
    # a sealed file for a passed-down phrase that names other strategies still fails
    other = {**forged, "confirmation": confirmation_phrase(["s9"])}
    gl._save("approval.json", {**other, "seal": _seal(other)})
    assert gl.guard("s1") and not approval_intact(other)
    assert not approval_intact({}) and not approval_intact({"strategies": None, "seal": ""})


def test_cli_confirmation_needs_a_person_at_a_terminal(capsys):
    phrase = confirmation_phrase(["s1"])
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return phrase

    assert cli.confirm_live_approval(["s1"], ask=ask, isatty=lambda: False) is None
    assert not asked and "needs a person" in capsys.readouterr().err
    assert cli.confirm_live_approval(["s1"], ask=lambda _p: "yes", isatty=lambda: True) is None

    def eof(_p):
        raise EOFError

    assert cli.confirm_live_approval(["s1"], ask=eof, isatty=lambda: True) is None
    assert cli.confirm_live_approval(["s1"], ask=ask, isatty=lambda: True) == phrase
    out = capsys.readouterr().out
    assert "REAL orders" in out and phrase in out and "promises" in out


def test_golive_approve_has_no_flag_that_skips_the_question(tmp_path, monkeypatch):
    p = cli.build_parser()
    for skip in ("--yes", "-y", "--force", "--confirm", "--no-input"):
        with pytest.raises(SystemExit):
            p.parse_args(["golive", "approve", "--strategy", "s1", "--by", "Shawn", skip])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_a: confirmation_phrase(["s1"]))  # piped answers don't count
    assert cli.main(["golive", "approve", "--strategy", "s1", "--by", "Shawn"]) == 1
    assert not (tmp_path / "data" / "golive" / "approval.json").exists()


def test_the_website_cannot_approve_live(tmp_path):
    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    app = create_app(Settings(db_path=str(tmp_path / "c.db")), Auth("admin", "pw-long-enough-1", "k"),
                     JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x")),
                     Paths(trade_dir=str(tmp_path / "t")))
    posts = [r.path for r in app.routes if "POST" in getattr(r, "methods", set())]
    assert posts and not [p for p in posts if "golive" in p or "approv" in p and "user" not in p]
