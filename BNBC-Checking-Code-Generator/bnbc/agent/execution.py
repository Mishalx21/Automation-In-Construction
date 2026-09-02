"""Execution node: run the candidate on the real corpus and through the
fixture gate, apply the deterministic gates (schema + AST), and classify a
failed gate as a code-level or oracle-level fault.

Real-model runs happen here in isolated subprocesses; fixture runs happen
inside the acceptance gate (``fixtures_facade.run_acceptance_gate``), and
their verdicts are folded into the candidate's verdict vector from the gate's
AcceptanceReport. This node is the pipeline's sole acceptance authority: it
re-runs the agent session's chosen candidate from scratch, so nothing the
drafter observed in its own loop can grant acceptance.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from bnbc import config as cfg
from bnbc.contracts import AcceptanceReport, FixtureManifest, Verdict, validate_check_result
from bnbc.agent import fixtures_facade

logger = logging.getLogger("bnbc.agent.execution")


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

def _run_checker_subprocess(code_path: Path, ifc_path: Path, timeout: int) -> dict:
    """Run one check_rule.py against one IFC file in an isolated subprocess."""
    wrapper = f"""\
import sys, json, importlib.util, ifcopenshell, time

sys.path.insert(0, {str(cfg.IFC_HELPERS_DIR.parent)!r})

spec = importlib.util.spec_from_file_location("check_rule", {str(code_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

model = ifcopenshell.open({str(ifc_path)!r})
t0 = time.perf_counter()
result = module.check_rule(model)
elapsed = int((time.perf_counter() - t0) * 1000)

print(json.dumps({{"result": result, "elapsed_ms": elapsed}}))
"""
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        wall_ms = int((time.perf_counter() - start) * 1000)
        if proc.returncode != 0:
            return {
                "success": False,
                "result": None,
                "error": proc.stderr.strip()[-1500:] if proc.stderr else "non-zero exit, no stderr",
                "execution_time_ms": wall_ms,
            }
        try:
            payload = json.loads(proc.stdout.strip())
            return {
                "success": True,
                "result": payload.get("result"),
                "error": None,
                "execution_time_ms": payload.get("elapsed_ms", wall_ms),
            }
        except json.JSONDecodeError as exc:
            return {
                "success": False,
                "result": None,
                "error": f"JSON decode error: {exc}. stdout: {proc.stdout[:500]}",
                "execution_time_ms": wall_ms,
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "result": None,
            "error": f"Timeout after {timeout}s",
            "execution_time_ms": timeout * 1000,
        }
    except Exception as exc:
        return {"success": False, "result": None, "error": str(exc), "execution_time_ms": 0}


def run_code_on_files(code: str, files: list[Path], timeout: int) -> list[dict]:
    """Execute checker ``code`` against each IFC file (worker pool, stable order)."""
    if not files:
        return []
    tmp_dir = Path(tempfile.mkdtemp(prefix="bnbc_v2_checker_"))
    tmp_file = tmp_dir / "check_rule.py"
    tmp_file.write_text(code, encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=min(cfg.EXEC_MAX_WORKERS, len(files))) as pool:
            results = list(pool.map(
                lambda f: {"file_name": f.name, **_run_checker_subprocess(tmp_file, f, timeout)},
                files,
            ))
        return results
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Deterministic AST gate (reimplemented from v1 DeterministicVerifier ideas)
# ---------------------------------------------------------------------------

def parse_ast_limitations(code: str) -> list[str]:
    """Static checks: slice caps on collections, silent excepts, banned imports."""
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except Exception as exc:
        return [f"AST parse error: {exc}"]

    for node in ast.walk(tree):
        # Numeric slice caps >= 10 look like element-collection sampling.
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            upper = node.slice.upper
            if (
                upper is not None
                and isinstance(upper, ast.Constant)
                and isinstance(upper.value, int)
                and upper.value >= 10
            ):
                errors.append(
                    f"slicing constraint [:{upper.value}] detected — checkers must "
                    "evaluate all elements"
                )
        # Silent exception swallowing.
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                    errors.append(
                        "silent try/except-pass handler detected — exceptions must "
                        "propagate or be handled transparently"
                    )
        # Banned imports.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("os", "subprocess", "shutil"):
                    errors.append(f"forbidden import '{alias.name}'")
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in ("os", "subprocess", "shutil"):
                errors.append(f"forbidden import from '{node.module}'")
    return errors


# ---------------------------------------------------------------------------
# Candidate scoring (elitism across the run)
# ---------------------------------------------------------------------------

#: Candidate fields snapshotted into ``state["best_candidate"]``.
_BEST_FIELDS = (
    "candidate_id", "model_name", "code", "acceptance", "kill_rate",
    "runtime_ms_max", "static_errors", "schema_errors", "real_results",
    "verdict_vector", "fault_class", "accepted", "agent_turns", "agent_inspects",
)


def candidate_score(cand: dict[str, Any] | None) -> tuple[int, float]:
    """Gate score: (fixtures ok, kill_rate). Higher is strictly better.

    Whole-module rewrites oscillate — an iteration can fix one fixture and
    break two (observed live: 6/11 -> 5/11 -> 5/11). Both the agent session
    and the graph therefore keep the BEST candidate seen so far, so a terminal
    rejection hands the best (not the last) draft to the human.
    """
    if not cand:
        return (0, 0.0)
    acceptance = cand.get("acceptance") or {}
    ok = sum(1 for o in acceptance.get("outcomes") or [] if o.get("ok"))
    return (ok, float(cand.get("kill_rate") or 0.0))


def best_of(cand: dict[str, Any], previous_best: dict[str, Any] | None) -> dict[str, Any]:
    """The better of the freshly executed candidate and the running best."""
    if previous_best and candidate_score(previous_best) >= candidate_score(cand):
        return previous_best
    return {k: cand.get(k) for k in _BEST_FIELDS}


# ---------------------------------------------------------------------------
# Fault classification (code-level vs oracle-level)
# ---------------------------------------------------------------------------

def classify_fault(report: AcceptanceReport) -> str:
    """Classify a failed gate as a ``"code"`` or ``"oracle"`` fault.

    Oracle-fault signature (observed live on 8.3.4.2): nearly all scored
    (fail/pass-expected) fixtures miss with ONE uniform wrong verdict class in
    {not_applicable, unknown}. That means the spec card's applicability
    conventions or prerequisites put its own fixtures out of scope — a defect
    upstream of the code, which no code edit can move (the verdict vector
    is frozen by construction). Everything else — mixed outcomes, tracebacks,
    schema/static errors — is code-level and goes to repair.
    """
    scored = [o for o in report.outcomes if o.expected_verdict in (Verdict.FAIL, Verdict.PASS)]
    missed = [o for o in scored if not o.ok]
    if not scored or not missed:
        return "code"
    if len(missed) / len(scored) < cfg.ORACLE_FAULT_MISS_RATIO:
        return "code"
    actuals = {o.actual_verdict for o in missed}
    if len(actuals) == 1 and actuals <= {Verdict.NOT_APPLICABLE, Verdict.UNKNOWN}:
        return "oracle"
    return "code"


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def execute_node(state: dict) -> dict:
    """Run the candidate: real models + fixture gate + deterministic gates."""
    rule_id = state["rule_id"]
    manifest = FixtureManifest.model_validate(
        state.get("fixture_manifest") or {"rule_id": rule_id, "fixtures": []}
    )
    timeout = cfg.CHECKER_TIMEOUT_SECONDS
    real_files = [Path(p) for p in (state.get("base_models") or [])]

    cand = dict(state.get("candidate") or {})
    if not cand:
        return {"status": "executed"}  # routing rejects on a missing candidate

    cid = cand["candidate_id"]
    code = cand.get("code", "")

    # The agent session ended without a parseable module: nothing to run, and
    # nothing a re-run could reveal. Routing turns this into a rejection.
    if not code:
        cand["accepted"] = False
        cand["fault_class"] = "code"
        cand["static_errors"] = ["agent session produced no parseable module"]
        cand["schema_errors"] = []
        cand["verdict_vector"] = {}
        return {"candidate": cand, "status": "executed"}

    # --- deterministic static gate ---
    static_errors = parse_ast_limitations(code)

    # --- real model runs (subprocesses) ---
    real_results = run_code_on_files(code, real_files, timeout)

    verdict_vector: dict[str, str] = {}
    schema_errors: list[str] = []
    for res in real_results:
        fname = res["file_name"]
        if res.get("success") and isinstance(res.get("result"), dict):
            errs = validate_check_result(res["result"])
            schema_errors.extend(f"{fname}: {e}" for e in errs)
            verdict_vector[fname] = str(res["result"].get("verdict", "error"))
        else:
            verdict_vector[fname] = "error"
            if res.get("error"):
                schema_errors.append(f"{fname}: execution failed — {res['error']}")

    # --- fixture acceptance gate (via facade) ---
    try:
        report = fixtures_facade.run_acceptance_gate(
            rule_id, code, manifest, [str(f) for f in real_files], timeout
        )
    except Exception as exc:
        logger.error("Acceptance gate failed for %s: %s", cid, exc)
        report = AcceptanceReport(
            rule_id=rule_id,
            candidate_id=cid,
            static_errors=[f"acceptance gate error: {exc}"],
            accepted=False,
        )
    report.candidate_id = cid
    report.schema_errors = list(report.schema_errors) + schema_errors
    report.static_errors = list(report.static_errors) + static_errors
    if report.schema_errors or report.static_errors:
        report.accepted = False

    for outcome in report.outcomes:
        verdict_vector[outcome.fixture_id] = (
            outcome.actual_verdict.value if outcome.actual_verdict else "error"
        )

    cand["static_errors"] = static_errors
    cand["schema_errors"] = schema_errors
    cand["real_results"] = real_results
    cand["verdict_vector"] = verdict_vector
    cand["acceptance"] = report.model_dump(mode="json")
    cand["kill_rate"] = report.kill_rate
    cand["runtime_ms_max"] = report.runtime_ms_max
    cand["accepted"] = report.accepted
    cand["fault_class"] = "" if report.accepted else classify_fault(report)
    if cand["fault_class"] == "oracle":
        logger.warning(
            "Candidate %s: oracle-fault signature — the spec/fixture contract, "
            "not the code, is the likely defect", cid,
        )

    return {
        "candidate": cand,
        "best_candidate": best_of(cand, state.get("best_candidate")),
        "status": "executed",
    }
