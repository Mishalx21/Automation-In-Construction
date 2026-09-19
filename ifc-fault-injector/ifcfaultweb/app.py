"""Web adapter for ifcfault.

The engine deliberately produces a reviewable standalone script. This layer
keeps that artifact and runs it in a subprocess to create the plain checker
fixture and a coloured human-review copy for each browser job.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import ifcopenshell
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ifcfault.emit import EmitError, emit, emit_multi
from ifcfault.inventory import model_inventory
from ifcfault.library import registry

MAX_UPLOAD_MB = float(os.environ.get("IFCFAULTWEB_MAX_UPLOAD_MB", "400"))
DATA_ROOT = Path(os.environ.get("IFCFAULTWEB_DATA_ROOT", "/app/out"))
TIMEOUT_SECONDS = int(os.environ.get("IFCFAULTWEB_TIMEOUT_SECONDS", "1800"))
XKT_CONVERTER = Path(os.environ.get(
    "IFCFAULTWEB_XKT_CONVERTER",
    "/opt/xeokit/node_modules/@xeokit/xeokit-convert/convert2xkt.js",
))
_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")

QUEUED_ANALYSIS, ANALYZING, CONVERTING_PREVIEW, READY = (
    "queued_analysis", "analyzing", "converting_preview", "ready"
)
QUEUED_INJECT, EMITTING, WRITING, VERIFYING, DONE, FAILED = (
    "queued_inject", "emitting", "writing_outputs", "verifying", "done", "failed"
)


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return (_SANITISE.sub("_", name).strip("._") or "upload")[:120]


def _checks(result: Any) -> list[dict[str, Any]]:
    return [asdict(check) for check in (result.checks if result else [])]


@dataclass
class Job:
    job_id: str
    filename: str
    root: Path
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    state: str = QUEUED_ANALYSIS
    error: str | None = None
    analysis: dict[str, Any] | None = None
    mutations: list[dict[str, Any]] | None = None
    verification: dict[str, Any] | None = None
    script_path: Path | None = None
    plain_path: Path | None = None
    coloured_path: Path | None = None
    record_path: Path | None = None
    source_preview_path: Path | None = None
    source_preview_error: str | None = None
    coloured_preview_path: Path | None = None
    coloured_preview_error: str | None = None

    @property
    def source_path(self) -> Path:
        return self.root / "source" / self.filename


class JobStore:
    """One sequential worker: IFC parsing and model generation are memory-heavy."""

    def __init__(self, root: Path):
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.work: queue.Queue[tuple[str, str, list[str] | None]] = queue.Queue()
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.worker or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._loop, name="ifcfault-web-worker", daemon=True)
            self.worker.start()

    def create(self, filename: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, filename=_safe_name(filename), root=self.root / job_id)
        job.source_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def set(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                for key, value in changes.items():
                    setattr(job, key, value)

    def enqueue_analysis(self, job_id: str) -> None:
        self.work.put(("analyze", job_id, None))

    def enqueue_injection(self, job_id: str, rules: list[str]) -> None:
        self.set(job_id, state=QUEUED_INJECT)
        self.work.put(("inject", job_id, rules))

    def _loop(self) -> None:
        while True:
            kind, job_id, rules = self.work.get()
            try:
                if kind == "analyze":
                    self._analyze(job_id)
                else:
                    self._inject(job_id, rules or [])
            except Exception as exc:  # never lose the service worker on one bad model
                self.set(job_id, state=FAILED, error=f"internal error: {exc!r}")
            finally:
                self.work.task_done()

    def _analyze(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        self.set(job_id, state=ANALYZING)
        try:
            model = ifcopenshell.open(str(job.source_path))
            entries: list[dict[str, Any]] = []
            for rule_id, rule in sorted(registry().items()):
                try:
                    applicable = rule.applicable(model)
                    candidates = rule.candidates(model) if applicable.ok else []
                    entries.append({
                        "rule_id": rule_id, "domain": getattr(rule, "DOMAIN", ""),
                        "clause": getattr(rule, "CLAUSE", ""),
                        "description": f"Inject a controlled {rule_id} violation.",
                        "applicable": applicable.ok, "reason": applicable.reason,
                        "candidate_count": len(candidates),
                    })
                except Exception as exc:
                    entries.append({"rule_id": rule_id, "domain": getattr(rule, "DOMAIN", ""),
                                    "clause": getattr(rule, "CLAUSE", ""),
                                    "description": f"Inject a controlled {rule_id} violation.",
                                    "applicable": False, "reason": f"query failed: {exc}",
                                    "candidate_count": 0})
            inventory = model_inventory(model, job.source_path)
            del model
            self.set(job_id, analysis={
                "schema_name": inventory.get("schema"),
                "file_size_bytes": job.source_path.stat().st_size,
                "rules": entries,
            })
            # XKT is preprocessed here, never in Chrome. A failed conversion
            # must not prevent a user from using the actual checking/injection
            # workflow, so it is exposed as an unavailable preview instead.
            self.set(job_id, state=CONVERTING_PREVIEW)
            try:
                preview = self._convert_xkt(job.source_path, job.root / "preview" / "source.xkt")
                self.set(job_id, source_preview_path=preview)
            except Exception as exc:
                self.set(job_id, source_preview_error=str(exc)[-1000:])
            self.set(job_id, state=READY)
        except Exception as exc:
            self.set(job_id, state=FAILED, error=f"could not analyze file: {exc}")

    @staticmethod
    def _convert_xkt(source: Path, output: Path) -> Path:
        """Convert outside the web process so a bad WASM conversion is isolated."""
        if not XKT_CONVERTER.exists():
            raise RuntimeError("server-side XKT converter is not installed")
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["node", "--no-experimental-fetch", str(XKT_CONVERTER), "-s", str(source), "-o", str(output)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            detail = completed.stderr or completed.stdout or "converter did not create an XKT file"
            raise RuntimeError(f"XKT conversion failed: {detail[-1000:]}")
        return output

    def _run_script(self, script: Path, source: Path, outdir: Path, coloured: bool) -> None:
        outdir.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(script), "--source", str(source), "--outdir", str(outdir),
                   "--colored" if coloured else "--no-color"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "generated script failed")[-3000:])

    def _inject(self, job_id: str, rule_ids: list[str]) -> None:
        job = self.get(job_id)
        if not job:
            return
        self.set(job_id, state=EMITTING)
        emitted = job.root / "emitted"
        try:
            if len(rule_ids) == 1:
                result = emit(source_ifc=job.source_path, rule_id=rule_ids[0], outdir=emitted,
                              timeout_s=TIMEOUT_SECONDS, validate_colored=True, log=lambda _: None)
            else:
                result = emit_multi(source_ifc=job.source_path, rule_ids=rule_ids, outdir=emitted,
                                    timeout_s=TIMEOUT_SECONDS, validate_colored=True, log=lambda _: None)
            if not result.ok or not result.script_path:
                raise EmitError(result.message or "generated script did not pass validation")

            self.set(job_id, state=WRITING, script_path=result.script_path)
            plain_dir, coloured_dir = job.root / "plain", job.root / "coloured"
            self._run_script(result.script_path, job.source_path, plain_dir, False)
            self._run_script(result.script_path, job.source_path, coloured_dir, True)

            plain = next((p for p in plain_dir.glob("*.ifc") if not p.name.endswith("_colored.ifc")), None)
            coloured = next(coloured_dir.glob("*_colored.ifc"), None)
            record = next(plain_dir.glob("*_record.json"), None)
            if not plain or not coloured or not record:
                raise RuntimeError("generated script did not produce all expected artifacts")
            record_data = json.loads(record.read_text(encoding="utf-8"))
            raw_mutations = record_data.get("mutations") or [record_data.get("mutation") or {}]
            mutations = [entry.get("mutation", entry) for entry in raw_mutations]
            verification = {"all_passed": True, "checks": _checks(result.validation),
                            "fault": result.validation.fault if result.validation else "none"}
            self.set(job_id, state=VERIFYING, mutations=mutations, verification=verification,
                     plain_path=plain, coloured_path=coloured, record_path=record)
            self.set(job_id, state=CONVERTING_PREVIEW)
            try:
                preview = self._convert_xkt(coloured, job.root / "preview" / "violating.xkt")
                self.set(job_id, coloured_preview_path=preview)
            except Exception as exc:
                self.set(job_id, coloured_preview_error=str(exc)[-1000:])
            self.set(job_id, state=DONE)
        except Exception as exc:
            self.set(job_id, state=FAILED, error=str(exc))


store = JobStore(DATA_ROOT)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.start()
    yield


app = FastAPI(title="ifcfault web", version="1.0.0", lifespan=lifespan)


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".ifc"):
        raise HTTPException(422, "only .ifc files are accepted")
    job = store.create(file.filename)
    total, limit = 0, int(MAX_UPLOAD_MB * 1024 * 1024)
    with job.source_path.open("wb") as target:
        while chunk := await file.read(1 << 20):
            total += len(chunk)
            if total > limit:
                target.close()
                shutil.rmtree(job.root, ignore_errors=True)
                raise HTTPException(413, f"file exceeds the {MAX_UPLOAD_MB:g} MB upload limit")
            target.write(chunk)
    if not total:
        raise HTTPException(422, "uploaded file is empty")
    store.enqueue_analysis(job.job_id)
    return {"job_id": job.job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job '{job_id}'")
    return {"job_id": job.job_id, "state": job.state, "filename": job.filename,
            "created_at": job.created_at, "error": job.error, "analysis": job.analysis,
            "mutations": job.mutations, "verification": job.verification,
            "output_size_bytes": job.plain_path.stat().st_size if job.plain_path and job.plain_path.exists() else None,
            "coloured_output_size_bytes": (
                job.coloured_path.stat().st_size if job.coloured_path and job.coloured_path.exists() else None
            ),
            "source_preview": {
                "state": "ready" if job.source_preview_path else ("unavailable" if job.source_preview_error else "pending"),
                "size_bytes": job.source_preview_path.stat().st_size if job.source_preview_path and job.source_preview_path.exists() else None,
            },
            "coloured_preview": {
                "state": "ready" if job.coloured_preview_path else ("unavailable" if job.coloured_preview_error else "pending"),
                "size_bytes": job.coloured_preview_path.stat().st_size if job.coloured_preview_path and job.coloured_preview_path.exists() else None,
            }}


@app.post("/api/jobs/{job_id}/inject")
def inject(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state != READY:
        raise HTTPException(409, f"job is '{job.state}', not ready")
    rules = [str(item.get("rule", "")).upper() for item in request.get("rules", [])]
    if not rules or any(not rule for rule in rules):
        raise HTTPException(422, "select at least one rule")
    available = {entry["rule_id"] for entry in (job.analysis or {}).get("rules", []) if entry["applicable"]}
    invalid = [rule for rule in rules if rule not in available]
    if invalid:
        raise HTTPException(422, f"selected rule is unavailable for this model: {', '.join(invalid)}")
    store.enqueue_injection(job_id, rules)
    return {"ok": True, "queued": rules}


def _artifact(job_id: str, attr: str, name: str) -> FileResponse:
    job = store.get(job_id)
    path = getattr(job, attr) if job else None
    if not job:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state != DONE or not path or not path.exists():
        raise HTTPException(409, "artifact is not ready")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@app.get("/api/jobs/{job_id}/download")
def download_plain(job_id: str):
    return _artifact(job_id, "plain_path", "violating.ifc")


@app.get("/api/jobs/{job_id}/download/colored")
def download_coloured(job_id: str):
    return _artifact(job_id, "coloured_path", "violating_colored.ifc")


@app.get("/api/jobs/{job_id}/download/script")
def download_script(job_id: str):
    return _artifact(job_id, "script_path", "ifcfault_injection.py")


@app.get("/api/jobs/{job_id}/download/report")
def download_report(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state != DONE or not job.verification:
        raise HTTPException(409, "report is not ready")
    return JSONResponse(job.verification, headers={"Content-Disposition": f'attachment; filename="{job_id}_validation.json"'})


@app.get("/api/jobs/{job_id}/preview/{kind}")
def download_preview(job_id: str, kind: str):
    if kind not in {"source", "violating"}:
        raise HTTPException(404, "unknown preview")
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job '{job_id}'")
    path = job.source_preview_path if kind == "source" else job.coloured_preview_path
    if not path or not path.exists():
        raise HTTPException(409, "server preview is not ready")
    return FileResponse(path, media_type="application/octet-stream", filename=f"{kind}.xkt")
