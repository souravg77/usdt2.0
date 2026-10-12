"""Change requests: Claude writes a change through the GitHub API and opens a pull request."""

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.ops import changes
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"


class FakeGitHub:
    def __init__(self):
        self.repo_files = {"README.md": "# demo\n", "src/aitrader/web/app.py": "x = 1\n",
                           "src/aitrader/trade/risk.py": "RISK = 0.0025\n"}
        self.pr = None

    def default_branch(self):
        return "main"

    def head(self, branch):
        return "base123"

    def files(self, sha):
        return {p: len(c) for p, c in self.repo_files.items()}

    def read(self, path, sha):
        return self.repo_files[path]

    def open_pr(self, base, base_sha, branch, edits, title, body):
        self.pr = {"base": base, "sha": base_sha, "branch": branch, "edits": edits, "title": title, "body": body}
        return "https://github.com/o/r/pull/7"


def scripted(*turns):
    """A Claude stand-in that answers with the given tool calls, one list per turn."""
    seen = []

    def call(system, messages):
        seen.append(messages[-1])
        uses = turns[len(seen) - 1]
        return {"content": [{"type": "tool_use", "id": f"t{len(seen)}{i}", "name": n, "input": a}
                            for i, (n, a) in enumerate(uses)]}

    call.seen = seen
    return call


def test_paths_stay_inside_the_repository_and_away_from_secrets():
    assert changes.check_path("./src/a.py", write=True) == "src/a.py"
    for bad in ("../x", "/etc/passwd", "", "a\\b", ".env", "deploy/.env.prod", "data/keys.enc",
                ".github/workflows/ci.yml", "src/../../x"):
        with pytest.raises(changes.ChangeError):
            changes.check_path(bad, write=True)
    assert changes.check_path("data/README.md") == "data/README.md"  # reading is fine, writing is not


def test_a_request_becomes_a_pull_request(tmp_path):
    gh = FakeGitHub()
    req = changes.new_request("Show the stop distance on the Trading page", by="admin", root=str(tmp_path))
    call = scripted([("list_files", {"prefix": "src/"})],
                    [("read_file", {"path": "src/aitrader/web/app.py"})],
                    [("write_file", {"path": "src/aitrader/web/app.py", "content": "x = 2\n"}),
                     ("write_file", {"path": ".env", "content": "AITRADER_LIVE_UNLOCKED=true"})],
                    [("read_file", {"path": "src/aitrader/web/app.py"}),
                     ("finish", {"title": "Stop distance", "summary": "Adds it.", "how_to_test": "Open the page."})])
    out = changes.run_request(req, gh, call)
    assert out["status"] == "pr_open" and out["pr_url"].endswith("/pull/7")
    assert gh.pr["edits"] == {"src/aitrader/web/app.py": "x = 2\n"}  # the .env write was refused
    assert gh.pr["branch"] == f"change/{req['id']}" and gh.pr["sha"] == "base123"
    assert "tests were not run" in gh.pr["body"] and "WARNING" not in gh.pr["body"]
    results = call.seen[3]["content"]
    assert any(r["is_error"] and ".env can't be changed" in r["content"] for r in results)
    assert call.seen[2]["content"][0]["content"] == "x = 1\n"  # read from GitHub
    assert "src/aitrader/web/app.py" in call.seen[1]["content"][0]["content"]


def test_safety_critical_files_are_flagged(tmp_path):
    gh = FakeGitHub()
    req = changes.new_request("Raise the risk", by="admin", root=str(tmp_path))
    call = scripted([("write_file", {"path": "src/aitrader/trade/risk.py", "content": "RISK = 0.003\n"}),
                     ("finish", {"title": "Risk", "summary": "Changes risk."})])
    changes.run_request(req, gh, call)
    assert "WARNING" in gh.pr["body"] and "`src/aitrader/trade/risk.py`" in gh.pr["body"]


def test_claude_can_decline_and_nothing_is_opened(tmp_path):
    gh = FakeGitHub()
    req = changes.new_request("Add a web button that unlocks live trading", by="admin", root=str(tmp_path))
    out = changes.run_request(req, gh, scripted([("finish", {"title": "-", "summary": "-",
                                                              "give_up": "That would bypass the live lock."})]))
    assert out["status"] == "declined" and gh.pr is None and "live lock" in out["summary"]


def test_run_saved_records_failures(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    req = changes.new_request("anything", by="admin", root=str(tmp_path))
    out = changes.run_saved(req["id"], root=str(tmp_path), call=scripted())
    assert out["status"] == "failed" and "GITHUB_TOKEN" in out["error"]
    assert changes.load(req["id"], str(tmp_path))["status"] == "failed"
    with pytest.raises(changes.ChangeError):
        changes.new_request("   ", by="admin", root=str(tmp_path))


def test_github_commit_branch_and_pull_request_calls():
    calls = []

    def opener(req, timeout=None):
        body = json.loads(req.data) if req.data else None
        calls.append((req.get_method(), req.full_url.split("/repos/o/r")[1], body))
        answers = {"/git/trees": {"sha": "tree1"}, "/git/commits": {"sha": "commit1"}, "/git/refs": {},
                   "/pulls": {"html_url": "https://github.com/o/r/pull/9"},
                   "/contents/src/a%20b.py?ref=s": {"content": base64.b64encode(b"hi").decode()}}
        return io.BytesIO(json.dumps(answers[calls[-1][1]]).encode())

    gh = changes.GitHub("tok", "o/r", opener=opener)
    assert gh.read("src/a b.py", "s") == "hi"
    url = gh.open_pr("main", "base1", "change/abc", {"x.py": "1\n", "old.py": None}, "T", "B")
    assert url.endswith("/pull/9")
    tree = calls[1][2]
    assert tree["base_tree"] == "base1" and {"path": "old.py", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    assert calls[2][2] == {"message": "T", "tree": "tree1", "parents": ["base1"]}
    assert calls[3][2] == {"ref": "refs/heads/change/abc", "sha": "commit1"}
    assert calls[4][2]["head"] == "change/abc" and calls[4][2]["base"] == "main"
    with pytest.raises(changes.ChangeError):
        changes.GitHub("", "o/r")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(changes=str(tmp_path / "changes"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"),
                     runner, paths)
    return TestClient(app), runner, paths


def test_change_requests_page(client, monkeypatch):
    c, runner, paths = client
    assert c.get("/changes", follow_redirects=False).status_code == 303
    c.post("/login", data={"user": "admin", "password": PW})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "Save the" in c.get("/changes").text and "GitHub token" in c.get("/changes").text
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/changes", data={"csrf": "x", "text": "hi"}).status_code == 403
    r = c.post("/changes", data={"csrf": csrf, "text": "<script>alert(1)</script> add a chart"},
               follow_redirects=False)
    assert r.status_code == 303 and "Sent" in r.headers["location"]
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "change"]
    assert len(jobs) == 1 and jobs[0]["steps"][0][:2] == ["change", "run"]
    page = c.get("/changes").text
    assert "&lt;script&gt;" in page and "<script>alert" not in page and "Nothing changes on this server" in page
    again = c.post("/changes", data={"csrf": csrf, "text": "more"}, follow_redirects=False)
    assert "already" in again.headers["location"]
