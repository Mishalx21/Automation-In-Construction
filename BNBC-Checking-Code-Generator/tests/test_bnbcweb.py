"""bnbcweb endpoint tests — FastAPI TestClient against the REAL accepted
checkers, on a minimal synthetic IFC authored by bnbc.fixtures.synthetic.

Checks run on the JobStore's worker thread, so these tests poll job state
exactly the way the frontend does — the same code path a browser takes.

The synthetic model has a project and units but no doors/beams/columns, so
checkers are expected to answer "not_applicable"/"pass" rather than find
violations: what these tests prove is the plumbing (upload -> queue ->
worker -> checker module -> result published), not rule semantics — those
are proven by the fixture gate during checker acceptance.
"""
import time

import pytest
from fastapi.testclient import TestClient

DONE_STATES = {"done", "failed"}
KNOWN_VERDICTS = {"pass", "fail", "violation", "unknown", "not_applicable", "error"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the job store pointed at a temp dir, so tests never
    write into the repo's out/."""
    from bnbcweb import app as app_module
    from bnbcweb.jobs import JobStore

    monkeypatch.setattr(app_module, "store", JobStore(tmp_path / "bnbcweb"))
    with TestClient(app_module.app) as c:
        yield c


def _write_bare_ifc(path):
    """A minimal valid IFC4 file (project + mm units, no elements)."""
    from bnbc.fixtures import synthetic

    model, _ctx = synthetic.make_model(units="mm")
    model.write(str(path))
    return path


def _upload(client: TestClient, path) -> str:
    with open(path, "rb") as f:
        res = client.post("/api/jobs", files={"file": (path.name, f, "application/octet-stream")})
    assert res.status_code == 200, res.text
    return res.json()["job_id"]


def _wait_done(client: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        assert res.status_code == 200, res.text
        job = res.json()
        if job["state"] in DONE_STATES:
            return job
        time.sleep(0.25)
    pytest.fail(f"job {job_id} never finished; last state: "
                f"{job and job['state']}, error: {job and job['error']}")


def test_checker_catalogue_lists_all_ten(client):
    res = client.get("/api/checkers")
    assert res.status_code == 200
    checkers = res.json()
    assert {c["rule_id"] for c in checkers} == \
           {f"A{i}" for i in range(1, 6)} | {f"S{i}" for i in range(1, 6)}
    assert all(c["domain"] in ("architectural", "structural") for c in checkers)
    assert all(c["title"] and c["checker_path"] for c in checkers)


def test_upload_rejects_non_ifc(client):
    res = client.post("/api/jobs", files={"file": ("model.txt", b"hello", "text/plain")})
    assert res.status_code == 422


def test_upload_rejects_empty_file(client, tmp_path):
    res = client.post("/api/jobs", files={"file": ("empty.ifc", b"", "application/octet-stream")})
    assert res.status_code == 422


def test_full_flow_single_checker(client, tmp_path):
    """The browser flow end-to-end: upload a bare model -> run A1 ->
    done with a 'not_applicable' verdict (no doors) and a downloadable report."""
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))

    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": ["A1"]})
    assert res.status_code == 200, res.text

    job = _wait_done(client, job_id)
    assert job["state"] == "done", job["error"]
    assert len(job["results"]) == 1
    result = job["results"][0]
    assert result["rule_id"] == "A1"
    assert result["verdict"] == "not_applicable"
    assert result["violation_count"] == 0
    assert result["duration_s"] >= 0
    assert result["report"]["verdict"] == "not_applicable"

    res = client.get(f"/api/jobs/{job_id}/download/report")
    assert res.status_code == 200
    report = res.json()
    assert report["results"][0]["rule_id"] == "A1"


def test_all_ten_checkers_run_on_bare_model(client, tmp_path):
    """Every accepted checker must load and answer SOMETHING sane on a bare
    model — no crashes, no invented violations."""
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    all_rules = [c["rule_id"] for c in client.get("/api/checkers").json()]

    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": all_rules})
    assert res.status_code == 200, res.text
    job = _wait_done(client, job_id, timeout=300.0)

    assert job["state"] == "done", job["error"]
    results = {r["rule_id"]: r for r in job["results"]}
    assert set(results) == set(all_rules)
    errors = {rid: r["error"] for rid, r in results.items() if r["verdict"] == "error"}
    assert not errors, f"checkers crashed on a bare model: {errors}"
    for r in results.values():
        assert r["verdict"] in KNOWN_VERDICTS - {"error", "fail", "violation"}, r


def test_check_rejects_unknown_rule(client, tmp_path):
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": ["Z9"]})
    assert res.status_code == 422
    assert "Z9" in res.json()["detail"]


def test_check_rejects_duplicates(client, tmp_path):
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": ["A1", "A1"]})
    assert res.status_code == 422


def test_check_rejects_empty_selection(client, tmp_path):
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": []})
    assert res.status_code == 422


def test_unknown_job_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.post("/api/jobs/deadbeef/check", json={"rules": ["A1"]}).status_code == 404
    assert client.get("/api/jobs/deadbeef/download/report").status_code == 404


def test_report_before_done_409(client, tmp_path):
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    res = client.get(f"/api/jobs/{job_id}/download/report")
    assert res.status_code == 409


def test_second_check_while_running_409(client, tmp_path, monkeypatch):
    """A check already in flight must refuse a second one (single worker)."""
    from bnbcweb import jobs as jobs_module

    # Freeze the job in 'checking': patch _set so state never reaches done.
    real_set = jobs_module.JobStore._set

    def frozen_set(self, job_id, **changes):
        if changes.get("state") == jobs_module.DONE:
            return
        real_set(self, job_id, **changes)

    monkeypatch.setattr(jobs_module.JobStore, "_set", frozen_set)
    job_id = _upload(client, _write_bare_ifc(tmp_path / "bare.ifc"))
    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": ["A1"]})
    assert res.status_code == 200

    deadline = time.time() + 30
    while time.time() < deadline:  # wait until the (never-finishing) check starts
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("queued_check", "checking"):
            break
        time.sleep(0.25)
    res = client.post(f"/api/jobs/{job_id}/check", json={"rules": ["A2"]})
    assert res.status_code == 409
