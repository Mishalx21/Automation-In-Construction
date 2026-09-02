"""Deterministic acceptance gate — the ONLY place a candidate is accepted.

A candidate checker is accepted iff, over the rule's self-verified fixture
manifest:

* every expected-``fail`` fixture is killed — verdict ``fail`` naming every
  expected condition id, with at least one expected element GUID among the
  violation locations;
* every compliant/edge fixture returns ``pass`` (zero false positives);
* every data-degraded fixture returns ``unknown`` and every out-of-scope
  fixture returns ``not_applicable``;
* every run finishes inside the timeout and every result validates against
  the CheckResultV2 schema;
* the manifest actually contains at least one expected-``fail`` fixture (an
  empty or fail-free manifest can never accept — no vacuous acceptance).

``real_models`` is accepted for interface completeness but intentionally not
re-executed here: the execution node already runs candidates on the real
corpus and merges those schema errors into the report. Re-running 20MB+
models inside the gate would double the wall-clock for zero extra signal.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bnbc.contracts import AcceptanceReport, FixtureManifest, FixtureOutcome, Verdict, \
    validate_check_result

logger = logging.getLogger("bnbc.fixtures.gate")


def _run_checker(code_path: Path, ifc_path: Path, timeout_s: int) -> dict:
    """One isolated subprocess run of check_rule.py against one IFC file."""
    from bnbc import config as cfg  # lazy: project root for ifc_helpers imports

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
            capture_output=True, text=True, timeout=timeout_s,
        )
        wall_ms = int((time.perf_counter() - start) * 1000)
        if proc.returncode != 0:
            return {"success": False, "result": None, "elapsed_ms": wall_ms,
                    "error": (proc.stderr or "non-zero exit, no stderr").strip()[-1500:]}
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            return {"success": False, "result": None, "elapsed_ms": wall_ms,
                    "error": f"JSON decode error: {exc}. stdout: {proc.stdout[:500]}"}
        return {"success": True, "result": payload.get("result"),
                "elapsed_ms": int(payload.get("elapsed_ms", wall_ms)), "error": None}
    except subprocess.TimeoutExpired:
        return {"success": False, "result": None, "elapsed_ms": timeout_s * 1000,
                "error": f"Timeout after {timeout_s}s"}
    except Exception as exc:
        return {"success": False, "result": None, "elapsed_ms": 0, "error": str(exc)}


def _checker_summary(raw: dict) -> str:
    """The checker's own account of what it did — the drafter pairs this
    with the fixture's measured ground truth (probe report)."""
    parts: list[str] = []
    summary = str(raw.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    coverage: list[str] = []
    for cid, cov in (raw.get("checked_summary") or {}).items():
        if not isinstance(cov, dict):
            continue
        line = f"{cid}: checked {cov.get('elements_checked', 0)}, " \
               f"skipped {cov.get('elements_skipped', 0)}"
        skips = cov.get("skip_reasons") or {}
        if skips:
            line += " (" + ", ".join(f"{k}: {v}" for k, v in skips.items()) + ")"
        coverage.append(line)
    if coverage:
        parts.append("; ".join(coverage))
    unknowns = "; ".join(
        f"missing {u.get('missing')}" for u in (raw.get("unknown_reasons") or [])
        if isinstance(u, dict)
    )
    if unknowns:
        parts.append(unknowns)
    return " | ".join(parts)[:400]


def _evaluate(spec, raw: dict) -> FixtureOutcome:
    """Score one fixture run against its expected verdict/conditions/elements."""
    outcome = FixtureOutcome(
        fixture_id=spec.fixture_id, expected_verdict=spec.expected_verdict
    )
    outcome.checker_summary = _checker_summary(raw)
    verdict_str = str(raw.get("verdict", ""))
    try:
        outcome.actual_verdict = Verdict(verdict_str)
    except ValueError:
        outcome.execution_error = f"unrecognised verdict {verdict_str!r}"
        return outcome

    if spec.expected_verdict != Verdict.FAIL:
        outcome.ok = outcome.actual_verdict == spec.expected_verdict
        return outcome

    violations = raw.get("violations") or []
    found_conditions = {str(v.get("condition", "")) for v in violations}
    location_texts = [
        str(loc.get("element", ""))
        for v in violations
        for loc in (v.get("locations") or [])
    ]
    outcome.conditions_hit = [c for c in spec.expected_conditions if c in found_conditions]
    outcome.conditions_missed = [c for c in spec.expected_conditions if c not in found_conditions]
    outcome.elements_hit = [
        g for g in spec.expected_elements if any(g in t for t in location_texts)
    ]
    outcome.elements_missed = [g for g in spec.expected_elements if g not in outcome.elements_hit]
    # Element matching is any-of: a spacing fixture may legitimately be
    # reported at either of the two bars involved. Conditions must ALL hit.
    outcome.ok = (
        outcome.actual_verdict == Verdict.FAIL
        and not outcome.conditions_missed
        and (not spec.expected_elements or bool(outcome.elements_hit))
    )
    return outcome


def run_gate(
    rule_id: str,
    code: str,
    manifest: FixtureManifest,
    real_models: list[str],
    timeout_s: int,
    fixtures_dir: Path | str | None = None,
) -> AcceptanceReport:
    """Run ``code`` over the fixture manifest and decide acceptance."""
    if fixtures_dir is not None:
        fdir = Path(fixtures_dir)
    else:
        from bnbc import config as cfg  # lazy
        fdir = cfg.FIXTURES_DIR / rule_id

    report = AcceptanceReport(rule_id=rule_id)
    if not any(f.expected_verdict == Verdict.FAIL for f in manifest.fixtures):
        report.static_errors.append(
            "manifest has no expected-fail fixture — acceptance impossible by design"
        )
        return report

    tmp_dir = Path(tempfile.mkdtemp(prefix="bnbc_gate_"))
    code_path = tmp_dir / "check_rule.py"
    code_path.write_text(code, encoding="utf-8")
    try:
        from bnbc import config as cfg  # lazy

        with ThreadPoolExecutor(
            max_workers=min(cfg.EXEC_MAX_WORKERS, max(len(manifest.fixtures), 1))
        ) as pool:
            runs = list(pool.map(
                lambda spec: (spec, _run_checker(code_path, fdir / spec.file_name, timeout_s)
                              if (fdir / spec.file_name).exists() else None),
                manifest.fixtures,
            ))
        for spec, res in runs:
            outcome = FixtureOutcome(
                fixture_id=spec.fixture_id, expected_verdict=spec.expected_verdict
            )
            if res is None:
                outcome.execution_error = f"fixture file missing: {fdir / spec.file_name}"
                report.outcomes.append(outcome)
                continue
            report.runtime_ms_max = max(report.runtime_ms_max, int(res.get("elapsed_ms", 0)))
            if not res.get("success") or not isinstance(res.get("result"), dict):
                outcome.execution_error = str(res.get("error") or "no result dict")
                report.outcomes.append(outcome)
                continue

            raw = res["result"]
            errs = validate_check_result(raw)
            report.schema_errors.extend(f"{spec.fixture_id}: {e}" for e in errs)
            report.outcomes.append(_evaluate(spec, raw))
    finally:
        try:
            code_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass

    report.accepted = (
        bool(report.outcomes)
        and all(o.ok for o in report.outcomes)
        and not report.schema_errors
        and not report.static_errors
    )
    logger.info(
        "Gate %s: %d/%d fixtures ok, kill_rate=%.2f, accepted=%s",
        rule_id, sum(1 for o in report.outcomes if o.ok), len(report.outcomes),
        report.kill_rate, report.accepted,
    )
    return report
