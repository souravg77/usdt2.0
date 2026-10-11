"""Versioned model registry with human promotion and instant rollback (roadmap Phase 4, step 6
and "Model promotion").

Layout under ``data/ai/models``::

    registry.json            active version, rollback stack, every version's status
    <version>/model.pkl      the fitted FilterModel
    <version>/report.json    test results (machine-readable)
    <version>/report.md      the side-by-side report a person reads before promoting

Statuses: ``candidate`` (beat the unfiltered rules, waiting for a person), ``rejected`` (did
not), ``active`` (in use), ``archived`` (was active). Training never promotes: only
``promote``, run by a person, makes a model active, and only a candidate can be promoted. The
previous active model stays on disk, so ``rollback`` switches back at once.

``model.pkl`` is a pickle. Only load models this registry wrote: its SHA-256 is recorded at save
time and checked before every load.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
from datetime import datetime, timezone

from .model import FilterModel, report_markdown

DEFAULT_DIR = "data/ai/models"
STATUSES = ("candidate", "rejected", "active", "archived")


class ModelRegistry:
    def __init__(self, root: str = DEFAULT_DIR):
        self.root = root
        self.path = os.path.join(root, "registry.json")
        self.data = {"version": 1, "active": None, "previous": [], "models": {}}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))

    # ---- queries --------------------------------------------------------------------------------

    @property
    def active(self) -> str | None:
        return self.data["active"]

    def info(self, version: str) -> dict:
        if version not in self.data["models"]:
            raise KeyError(f"no model {version!r}")
        return self.data["models"][version]

    def versions(self) -> list[str]:
        return sorted(self.data["models"])

    def report(self, version: str) -> dict:
        with open(os.path.join(self.root, version, "report.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def report_text(self, version: str) -> str:
        with open(os.path.join(self.root, version, "report.md"), encoding="utf-8") as fh:
            return fh.read()

    def load(self, version: str) -> FilterModel:
        meta = self.info(version)
        path = os.path.join(self.root, version, "model.pkl")
        with open(path, "rb") as fh:
            blob = fh.read()
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            raise RuntimeError(f"{path} does not match the checksum in the registry; not loading it")
        return pickle.loads(blob)  # noqa: S301 - our own file, checksum verified above

    def load_active(self) -> tuple[str, FilterModel] | None:
        if not self.active:
            return None
        return self.active, self.load(self.active)

    # ---- changes --------------------------------------------------------------------------------

    def add(self, model: FilterModel, report: dict, dataset: dict | None = None) -> str:
        """Store a freshly trained model as a candidate (or rejected, if it failed its test)."""
        version = datetime.now(timezone.utc).strftime("m%Y%m%d-%H%M%S")
        while version in self.data["models"]:
            version += "x"
        folder = os.path.join(self.root, version)
        os.makedirs(folder, exist_ok=True)
        blob = pickle.dumps(model)
        _write(os.path.join(folder, "model.pkl"), blob)
        _write(os.path.join(folder, "report.json"), json.dumps(report, indent=1, default=str).encode())
        _write(os.path.join(folder, "report.md"),
               report_markdown(version, report, self.active).encode())
        status = "candidate" if report["passed"] else "rejected"
        t = report["test"]
        self.data["models"][version] = {
            "status": status, "created": time.time(), "sha256": hashlib.sha256(blob).hexdigest(),
            "threshold": report["threshold"], "dataset": dataset or {},
            "test": {k: {m: t[k][m] for m in ("trades", "expectancy_r", "max_drawdown_r")}
                     for k in ("unfiltered", "filtered")},
            "why": "; ".join(report["failed"]),
            "history": [{"at": time.time(), "to": status, "why": "trained"}],
        }
        self.save()
        return version

    def promote(self, version: str, approved_by: str) -> None:
        meta = self.info(version)
        if meta["status"] != "candidate":
            raise ValueError(f"{version} is {meta['status']}; only a candidate that beat the "
                             "unfiltered rules can be promoted")
        if not approved_by:
            raise ValueError("a person must approve a promotion")
        old = self.active
        if old:
            self._move(old, "archived", f"replaced by {version}")
            self.data["previous"].append(old)
        self._move(version, "active", f"approved by {approved_by}")
        meta["approved_by"], meta["approved_at"] = approved_by, time.time()
        self.data["active"] = version
        self.save()

    def rollback(self, by: str = "") -> str | None:
        """Switch back to the model that was active before. Returns the new active version
        (None means the filter is off again)."""
        cur = self.active
        if not cur:
            raise ValueError("no active model to roll back")
        self._move(cur, "archived", f"rolled back{' by ' + by if by else ''}")
        prev = self.data["previous"].pop() if self.data["previous"] else None
        if prev:
            self._move(prev, "active", f"restored by rollback{' by ' + by if by else ''}")
        self.data["active"] = prev
        self.save()
        return prev

    def deactivate(self, by: str = "") -> None:
        """Turn the filter off (take every signal again) without deleting anything."""
        cur = self.active
        if cur:
            self._move(cur, "archived", f"filter turned off{' by ' + by if by else ''}")
            self.data["previous"].append(cur)
            self.data["active"] = None
            self.save()

    def _move(self, version: str, status: str, why: str) -> None:
        meta = self.info(version)
        meta["history"].append({"at": time.time(), "from": meta["status"], "to": status, "why": why})
        meta["status"] = status

    def save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        _write(self.path, json.dumps(self.data, indent=1, default=str).encode())

    def mtime(self) -> float:
        return os.path.getmtime(self.path) if os.path.exists(self.path) else 0.0


def _write(path: str, blob: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)  # atomic, so the paper loop never reads half a file
