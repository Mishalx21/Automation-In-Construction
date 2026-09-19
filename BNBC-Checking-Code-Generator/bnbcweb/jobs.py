"""Job store + single background worker for the bnbc-web layer.

Same deliberate design as the fault-injection service: ifcopenshell holds
whole files in memory (sources run to 342 MB), so HTTP handlers never open a
model — they enqueue and the browser polls. One worker thread, strictly
sequential, so the server can never blow up memory by opening several
large models at once.

The worker runs each selected checker exactly the way the acceptance
harness does (agentic_pipeline_v3/harness.py): import the module from
its path, open the model fresh per checker (a buggy checker cannot leak
state into the next one), call `check_rule(model)`, keep the result
dict verbatim.
"""
from __future__ import annotations

import importlib.util
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bnbcweb.schemas import CheckerResult, discover_checkers

# States, in the order a happy-path job moves through them.
UPLOADED = "uploaded"       # file stored, awaiting a check request
QUEUED = "queued_check"
CHECKING = "checking"
DONE = "done"
FAILED = "failed"

_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    """Strip anything path-like out of a client-supplied filename."""
    base = Path(name.replace("\\", "/")).name
    base = _SANITISE.sub("_", base).strip("._") or "upload"
    return base[:120]


@dataclass
class Job:
    job_id: str
    filename: str
    upload_dir: Path
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    state: str = UPLOADED
    error: Optional[str] = None
    results: list[CheckerResult] = field(default_factory=list)

    @property
    def source_path(self) -> Path:
        return self.upload_dir / self.filename


class JobStore:
    """In-memory job registry + one sequential worker thread."""

    def __init__(self, data_root: Path, ttl_hours: float = 24.0):
        self.data_root = Path(data_root)
        self.uploads_root = self.data_root / "uploads"
        self.ttl_seconds = ttl_hours * 3600.0
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        # Catalogue is static (checkers on disk), so resolve once; the
        # worker re-checks by path at run time.
        self._checkers = {c.rule_id: c for c in discover_checkers()}

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name="bnbcweb-worker", daemon=True)
            self._worker.start()

    def create(self, filename: str) -> tuple[Job, Path]:
        """Register a new job; the caller streams the upload to the returned
        path (under a fresh uuid dir, so no two uploads ever collide)."""
        self._evict_expired()
        job_id = uuid.uuid4().hex[:12]
        upload_dir = self.uploads_root / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        job = Job(job_id=job_id, filename=sanitize_filename(filename),
                  upload_dir=upload_dir)
        with self._lock:
            self._jobs[job_id] = job
        return job, upload_dir / job.filename

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    @property
    def checker_ids(self) -> set[str]:
        return set(self._checkers)

    @property
    def checkers(self):
        """The accepted-checker catalogue, as a mapping rule_id -> CheckerInfo."""
        return dict(self._checkers)

    # -- queueing ------------------------------------------------------------
    def enqueue_check(self, job_id: str, rule_ids: list[str]) -> None:
        self._set(job_id, state=QUEUED, results=[], error=None)
        self._queue.put(("check", job_id, rule_ids))

    def _set(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in changes.items():
                setattr(job, k, v)

    # -- worker --------------------------------------------------------------
    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item[0] == "check":
                    self._check(item[1], item[2])
            except Exception as e:  # one bad file must never kill the worker
                try:
                    self._set(item[1], state=FAILED, error=f"internal error: {e!r}")
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def _check(self, job_id: str, rule_ids: list[str]) -> None:
        import ifcopenshell

        job = self.get(job_id)
        if job is None:
            return
        self._set(job_id, state=CHECKING)

        results: list[CheckerResult] = []
        for rule_id in rule_ids:
            info = self._checkers.get(rule_id)
            if info is None:  # validated at request time; guard anyway
                results.append(CheckerResult(rule_id=rule_id, verdict="error",
                                             duration_s=0.0, error="unknown rule"))
                continue

            checker_path = Path(info.checker_path)
            if not checker_path.is_absolute():
                from bnbcweb.schemas import REPO_ROOT
                checker_path = REPO_ROOT / checker_path

            started = time.monotonic()
            try:
                # Same load discipline as the harness's _load_checker_module.
                # The checker itself inserts the repo root into sys.path to
                # reach ifc_helpers, so imports resolve wherever we run from.
                spec = importlib.util.spec_from_file_location(
                    f"bnbcweb_{rule_id}_{job_id}", checker_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                # Fresh open per checker: a checker that (buggily) mutates
                # the model cannot poison the next rule's verdict.
                model = ifcopenshell.open(str(job.source_path))
                try:
                    report = mod.check_rule(model)
                finally:
                    del model

                results.append(CheckerResult(
                    rule_id=rule_id,
                    verdict=str(report.get("verdict", "unknown")),
                    violation_count=report.get("violation_count"),
                    summary=report.get("summary"),
                    duration_s=round(time.monotonic() - started, 2),
                    report=report,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(CheckerResult(
                    rule_id=rule_id, verdict="error", duration_s=round(time.monotonic() - started, 2),
                    error=f"checker raised: {exc!r}",
                ))
            # Publish progressively so the UI can stream per-rule results.
            self._set(job_id, results=list(results))

        self._set(job_id, state=DONE)

    # -- housekeeping ----------------------------------------------------------
    def _evict_expired(self) -> None:
        """Remove job state + on-disk uploads older than the TTL. Runs on
        every new upload — keeps a long-running server's disk bounded
        without a separate scheduler."""
        cutoff = time.time() - self.ttl_seconds
        with self._lock:
            stale = [j for j in self._jobs.values()
                     if j.created_at and time.mktime(time.strptime(j.created_at, "%Y-%m-%dT%H:%M:%S")) < cutoff]
            for j in stale:
                self._jobs.pop(j.job_id, None)
        for j in stale:
            shutil.rmtree(j.upload_dir, ignore_errors=True)
