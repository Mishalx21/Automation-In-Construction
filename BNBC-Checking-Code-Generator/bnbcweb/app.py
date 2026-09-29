"""
FastAPI application — the web layer over the accepted BNBC checkers.

Every endpoint is JSON except the file downloads. Long work (opening the
model, running each checker) happens on the JobStore's single worker
thread; handlers enqueue and return immediately.

Run:  uvicorn bnbcweb.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from bnbcweb import jobs as J
from bnbcweb.jobs import JobStore
from bnbcweb.pdf_report import build_pdf
from bnbcweb.schemas import CheckRequest, CheckerInfo, JobStatus

REPO_ROOT = Path(__file__).resolve().parent.parent

MAX_UPLOAD_MB = float(os.environ.get("BNBCWEB_MAX_UPLOAD_MB", "400"))
DATA_ROOT = Path(os.environ.get("BNBCWEB_DATA_ROOT") or (REPO_ROOT / "out" / "bnbcweb"))

store = JobStore(DATA_ROOT)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not store.checker_ids:
        # Fail loudly at startup rather than serving an empty menu: the
        # accepted checkers ARE this service's product.
        raise RuntimeError(
            "no accepted checkers found — expected rule-*/check_*.py under "
            f"{REPO_ROOT}. The web layer only RUNS accepted checkers; generate "
            "them offline with the agentic pipeline first.")
    store.start()
    yield


app = FastAPI(title="bnbc-web", version="1.0.0", lifespan=lifespan)


# --- catalogue -------------------------------------------------------------------
@app.get("/api/checkers")
def list_checkers() -> list[CheckerInfo]:
    """The accepted-checker catalogue — the fixed menu the UI offers."""
    return sorted(store.checkers.values(), key=lambda c: (c.domain, c.rule_id))


# --- upload + job lifecycle --------------------------------------------------------
@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".ifc"):
        raise HTTPException(422, "only .ifc files are accepted")

    job, stored_path = store.create(file.filename)
    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    with open(stored_path, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                out.close()
                stored_path.unlink(missing_ok=True)
                raise HTTPException(413, f"file exceeds the {MAX_UPLOAD_MB:g} MB upload limit")
            out.write(chunk)
    if size == 0:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(422, "uploaded file is empty")

    return {"job_id": job.job_id}


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str) -> JobStatus:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"no such job '{job_id}'")
    return JobStatus(
        job_id=job.job_id, state=job.state, filename=job.filename,
        created_at=job.created_at, error=job.error,
        results=job.results or None,
    )


@app.post("/api/jobs/{job_id}/check")
def check(job_id: str, req: CheckRequest) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state in (J.QUEUED, J.CHECKING):
        raise HTTPException(409, f"a check is already running (state '{job.state}')")
    if not req.rules:
        raise HTTPException(422, "no rules requested")
    if len(set(req.rules)) != len(req.rules):
        raise HTTPException(422, "duplicate rule in request")
    unknown = [r for r in req.rules if r not in store.checker_ids]
    if unknown:
        raise HTTPException(422, f"unknown rule(s): {', '.join(unknown)}")

    store.enqueue_check(job_id, list(req.rules))
    return {"ok": True, "queued": req.rules}


# --- downloads --------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/download/report")
def download_report(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state != J.DONE or not job.results:
        raise HTTPException(409, "no report yet — job is not 'done'")
    payload = {
        "job_id": job.job_id,
        "filename": job.filename,
        "created_at": job.created_at,
        "results": [r.model_dump() for r in job.results],
    }
    return JSONResponse(content=payload, headers={
        "Content-Disposition": f'attachment; filename="{job.job_id}_bnbc_report.json"',
    })


@app.get("/api/jobs/{job_id}/download/report.pdf")
def download_report_pdf(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"no such job '{job_id}'")
    if job.state != J.DONE or not job.results:
        raise HTTPException(409, "no report yet — job is not 'done'")
    pdf_bytes = build_pdf(job.filename, job.created_at, job.results, store.checkers)
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{job.job_id}_bnbc_report.pdf"',
    })
