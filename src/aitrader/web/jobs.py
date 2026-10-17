"""Background jobs started from the dashboard: one at a time, each a separate ``aitrader``
process whose output goes to a log file, so a failing book never takes the dashboard down.

A book job runs ``kb build`` (so the book becomes searchable) and ``learn read <book> --test``
(Claude writes strategies from it, then each goes through the backtest gate; the ones that pass
move to paper trading). Job records live in ``data/web/jobs``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

log = logging.getLogger(__name__)

DEFAULT_DIR = "data/web/jobs"
STEP_TIMEOUT_S = 3 * 3600


class JobRunner:
    def __init__(self, root: str = DEFAULT_DIR, env_file: str = ".env"):
        self.root, self.env_file = root, env_file
        os.makedirs(root, exist_ok=True)
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, daemon=True, name="dashboard-jobs")
        self._worker.start()
        for job in self.list():  # a restart interrupted these
            if job["status"] in ("queued", "running"):
                self._update(job["id"], status="failed", error="the dashboard restarted during this job")

    # ---- records --------------------------------------------------------------------------------

    def _path(self, job_id: str, ext: str = "json") -> str:
        if not job_id.isalnum():
            raise KeyError(job_id)
        return os.path.join(self.root, f"{job_id}.{ext}")

    def get(self, job_id: str) -> dict:
        with open(self._path(job_id), encoding="utf-8") as fh:
            return json.load(fh)

    def log_text(self, job_id: str, tail: int = 200_000) -> str:
        path = self._path(job_id, "log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        return text[-tail:]

    def list(self, limit: int = 50) -> list[dict]:
        jobs = []
        for name in os.listdir(self.root):
            if name.endswith(".json"):
                try:
                    jobs.append(self.get(name[:-5]))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(jobs, key=lambda j: j["created"], reverse=True)[:limit]

    def _write(self, job: dict) -> None:
        tmp = self._path(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job, fh, indent=1)
        os.replace(tmp, self._path(job["id"]))

    def _update(self, job_id: str, **changes) -> dict:
        with self._lock:
            job = self.get(job_id)
            job.update(changes)
            self._write(job)
            return job

    # ---- running --------------------------------------------------------------------------------

    def submit(self, title: str, steps: list[list[str]], meta: dict | None = None) -> str:
        """Queue a job. Each step is the argument list for ``aitrader`` (no shell)."""
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "title": title, "steps": steps, "status": "queued",
               "created": time.time(), "started": None, "finished": None, "error": "",
               "meta": meta or {}}
        with self._lock:
            self._write(job)
        self._q.put(job_id)
        return job_id

    def command(self, step: list[str]) -> list[str]:
        return [sys.executable, "-m", "aitrader.cli", "--env-file", self.env_file, *step]

    def _loop(self) -> None:
        while True:
            job_id = self._q.get()
            try:
                self._run(job_id)
            except Exception as exc:  # keep the worker alive
                log.exception("dashboard job %s crashed", job_id)
                self._update(job_id, status="failed", error=str(exc)[:500], finished=time.time())

    def _run(self, job_id: str) -> None:
        job = self._update(job_id, status="running", started=time.time())
        with open(self._path(job_id, "log"), "a", encoding="utf-8") as out:
            for step in job["steps"]:
                out.write(f"$ aitrader {' '.join(step)}\n")
                out.flush()
                try:
                    rc = subprocess.run(self.command(step), stdout=out, stderr=subprocess.STDOUT,
                                        timeout=STEP_TIMEOUT_S, check=False).returncode
                except subprocess.TimeoutExpired:
                    rc = "timeout"
                out.write(f"[exit {rc}]\n\n")
                out.flush()
                if rc != 0:
                    self._update(job_id, status="failed", finished=time.time(),
                                 error=f"step `{step[0]} {step[1] if len(step) > 1 else ''}` ended with {rc}")
                    return
        self._update(job_id, status="done", finished=time.time())

    def wait_idle(self, timeout: float = 30) -> None:
        """For tests: block until every queued job has finished."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.empty() and all(j["status"] not in ("queued", "running") for j in self.list()):
                return
            time.sleep(0.05)
        raise TimeoutError("jobs still running")
