"""deploy/update.sh against a throwaway git remote and a fake `docker` that records its calls."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
built() { [ -f "$FAKE_STATE/built" ]; }
case "$*" in
  "image inspect"*) exit 0 ;;
  "tag "*) exit 0 ;;
  "compose build"*) touch "$FAKE_STATE/built"; exit 0 ;;
  "compose ps --services --status running") printf 'collector\npaper\n' ;;
  "compose ps --format"*)
    if built && [ -n "$FAKE_BAD_SERVICE" ]; then printf 'collector running\npaper restarting\n'
    else printf 'collector running\npaper running\n'; fi ;;
  "compose run --rm -T collector status")
    echo "mode: paper"
    if built && [ -n "$FAKE_UNLOCK_AFTER" ]; then echo "live trading: UNLOCKED"; else echo "live trading: locked"; fi ;;
  "compose run --rm -T collector backup "*) mkdir -p "${@: -1}"; echo db > "${@: -1}/aitrader.db" ;;
  "compose run --rm -T collector migrate") exit "${FAKE_MIGRATE_RC:-0}" ;;
  *) exit 0 ;;
esac
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def server(tmp_path):
    if not shutil.which("flock"):
        pytest.skip("flock not installed")
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(work, "config", k, v)
    (work / "deploy").mkdir()
    shutil.copy(SCRIPT, work / "deploy" / "update.sh")
    (work / ".gitignore").write_text("data/\n.env\n")
    (work / "app.txt").write_text("v1\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "v1")
    git(work, "push", "-q", "origin", "main")
    # a second clone plays the developer who merges a new version to main
    dev = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(dev, "config", k, v)
    (work / "data").mkdir()
    (work / "data" / "aitrader.db").write_text("live data\n")
    (work / ".env").write_text("AITRADER_LIVE_UNLOCKED=false\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(tmp_path / "docker.log"),
           "FAKE_STATE": str(state), "AITRADER_UPDATE_WAIT": "0"}

    def release(version):
        (dev / "app.txt").write_text(f"{version}\n")
        git(dev, "commit", "-qam", version)
        git(dev, "push", "-q", "origin", "main")

    def run(*args, **extra):
        (state / "built").unlink(missing_ok=True)
        return subprocess.run(["bash", "deploy/update.sh", *args], cwd=work, env={**env, **extra},
                              capture_output=True, text=True)

    def calls():
        p = tmp_path / "docker.log"
        return p.read_text().splitlines() if p.exists() else []

    return work, release, run, calls


def test_nothing_to_do(server):
    work, _release, run, calls = server
    r = run()
    assert r.returncode == 0 and "already up to date" in r.stdout
    assert not any("build" in c for c in calls())


def test_check_reports_without_changing_anything(server):
    work, release, run, calls = server
    release("v2")
    r = run("--check")
    assert r.returncode == 0 and "1 new commit" in r.stdout and "v2" in r.stdout
    assert (work / "app.txt").read_text() == "v1\n" and not calls()


def test_update_rebuilds_migrates_and_restarts(server):
    work, release, run, calls = server
    release("v2")
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (work / "app.txt").read_text() == "v2\n"
    c = calls()
    order = [next(i for i, x in enumerate(c) if x.startswith(p)) for p in
             ("docker compose run --rm -T collector backup", "docker compose build",
              "docker compose run --rm -T collector migrate", "docker compose up -d")]
    assert order == sorted(order)
    assert "docker compose up -d --remove-orphans collector paper" in c  # only what was running
    assert "live trading: locked" in r.stdout and list((work / "data" / "backups").iterdir())
    assert (work / ".env").read_text() == "AITRADER_LIVE_UNLOCKED=false\n"


@pytest.mark.parametrize("extra, why", [
    ({"FAKE_MIGRATE_RC": "3"}, "step failed"),
    ({"FAKE_BAD_SERVICE": "1"}, "service 'paper' is 'restarting'"),
    ({"FAKE_UNLOCK_AFTER": "1"}, "live trading reads 'UNLOCKED'"),
])
def test_failed_update_rolls_back(server, extra, why):
    work, release, run, calls = server
    before = git(work, "rev-parse", "HEAD")
    release("v2")
    (work / "data" / "aitrader.db").write_text("live data\n")
    r = run(**extra)
    assert r.returncode == 1 and why in r.stdout and "rolled back" in r.stdout, r.stdout + r.stderr
    assert git(work, "rev-parse", "HEAD") == before and (work / "app.txt").read_text() == "v1\n"
    c = calls()
    assert "docker tag aitrader:rollback aitrader:latest" in c
    assert c[-1] == "docker compose up -d collector paper"
    # the database comes back from the backup taken before the migration
    assert (work / "data" / "aitrader.db").read_text() == "db\n"


def test_refuses_with_local_edits(server):
    work, release, run, _calls = server
    release("v2")
    (work / "app.txt").write_text("edited on the server\n")
    r = run()
    assert r.returncode == 1 and "commit or 'git stash'" in r.stdout
