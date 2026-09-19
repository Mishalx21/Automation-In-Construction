"""Async HTTP wrapper around the bounded BNBC agentic generation graph.

The gateway authenticates requests before they arrive here. One worker runs
at a time because each generation has an LLM budget and opens IFC models.
Generated code is never automatically promoted to the compliance catalogue:
only an accepted artifact is made available for download and review.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from bnbc import config as cfg
from bnbc.agent.graph import run_rule
from bnbc.llm import LLMError, get_provider


RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ARTIFACTS_DIR = Path(os.environ.get("BNBC_GENERATOR_ARTIFACTS", cfg.ARTIFACTS_DIR))
cfg.ARTIFACTS_DIR = ARTIFACTS_DIR


class GenerateRequest(BaseModel):
    rule_id: str = Field(min_length=1, max_length=80, description="Stable ID, e.g. 8.3.5.1")
    title: str = Field(min_length=3, max_length=180)
    statement: str = Field(min_length=20, max_length=20_000)
    scope_note: str = Field(default="", max_length=4_000)
    source_clauses: list[str] = Field(default_factory=list, max_length=30)
    terms: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("rule_id")
    @classmethod
    def valid_rule_id(cls, value: str) -> str:
        value = value.strip()
        if not RULE_ID_RE.fullmatch(value):
            raise ValueError("use letters, digits, dots, dashes or underscores only")
        return value

    @field_validator("source_clauses", "terms")
    @classmethod
    def clean_values(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v.strip()]


@dataclass
class GenerationJob:
    job_id: str
    request: GenerateRequest
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: str = "queued"
    error: str = ""
    final_status: str = ""
    token_usage: dict = field(default_factory=dict)

    def payload(self) -> dict:
        artifact_dir = ARTIFACTS_DIR / self.request.rule_id
        accepted = self.final_status == "stored" and (artifact_dir / "checker.py").exists()
        return {
            "job_id": self.job_id,
            "rule_id": self.request.rule_id,
            "title": self.request.title,
            "state": self.state,
            "created_at": self.created_at,
            "error": self.error or None,
            "accepted": accepted,
            "token_usage": self.token_usage or None,
            "downloads": {
                "checker": f"/api/jobs/{self.job_id}/download/checker" if accepted else None,
                "acceptance": f"/api/jobs/{self.job_id}/download/acceptance" if accepted else None,
                "rejection": f"/api/jobs/{self.job_id}/download/rejection" if self.state == "rejected" else None,
            },
        }


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, GenerationJob] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self.worker = threading.Thread(target=self._run, name="bnbc-generator-worker", daemon=True)
        self.worker.start()

    def create(self, request: GenerateRequest) -> GenerationJob:
        job = GenerationJob(job_id=uuid.uuid4().hex, request=request)
        with self.lock:
            self.jobs[job.job_id] = job
        self.queue.put(job.job_id)
        return job

    def get(self, job_id: str) -> GenerationJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _run(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            job.state = "running"
            try:
                # Provider preflight gives a clear configuration failure before
                # the fixture engine spends minutes preparing a generation.
                get_provider().preflight()
                result = asyncio.run(run_rule(
                    job.request.rule_id,
                    rule_statement=job.request.statement,
                    rule_title=job.request.title,
                    rule_scope_note=job.request.scope_note,
                    source_clauses=job.request.source_clauses,
                    rule_terms=job.request.terms,
                    artifacts_dir=str(ARTIFACTS_DIR),
                ))
                job.final_status = str(result.get("status", "failed"))
                job.token_usage = result.get("token_usage") or {}
                if job.final_status == "stored":
                    job.state = "done"
                else:
                    job.state = "rejected"
                    job.error = str(result.get("escalation_reason") or "The fixture gate did not accept this checker.")
            except LLMError as exc:
                job.state = "failed"
                job.error = f"LLM configuration or provider error: {exc}"
            except Exception as exc:  # Worker must survive a single bad request.
                job.state = "failed"
                job.error = str(exc)
            finally:
                self.queue.task_done()


store = JobStore()
app = FastAPI(title="BNBC checker generator", version="1.0.0")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "queued": store.queue.qsize(), "artifacts_dir": str(ARTIFACTS_DIR)}


@app.post("/api/jobs")
def create_job(request: GenerateRequest) -> dict:
    job = store.create(request)
    return job.payload()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "generation job not found")
    return job.payload()


def job_artifact(job_id: str, name: str, download_name: str) -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "generation job not found")
    path = ARTIFACTS_DIR / job.request.rule_id / name
    if not path.is_file():
        raise HTTPException(409, "requested artifact is not available")
    return FileResponse(path, filename=download_name, media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/download/checker")
def download_checker(job_id: str):
    job = store.get(job_id)
    if job is None or job.state != "done":
        raise HTTPException(409, "an accepted checker is not available yet")
    return job_artifact(job_id, "checker.py", f"{job.request.rule_id}_checker.py")


@app.get("/api/jobs/{job_id}/download/acceptance")
def download_acceptance(job_id: str):
    return job_artifact(job_id, "acceptance.json", "acceptance.json")


@app.get("/api/jobs/{job_id}/download/rejection")
def download_rejection(job_id: str):
    job = store.get(job_id)
    if job is None or job.state != "rejected":
        raise HTTPException(409, "a rejection report is not available")
    return job_artifact(job_id, "rejection.json", "rejection.json")
