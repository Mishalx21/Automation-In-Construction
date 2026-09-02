"""Conformance review: ONE bounded structured ADVISORY call to a distinct
model, run only AFTER gate acceptance and entirely off the critical
path. The reviewer sees the spec card, the accepted code, and SUMMARIZED
results (never raw dumps). Its findings are provenance flags: they never veto
the gate, never route anywhere, and a reviewer failure never blocks storage.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from bnbc import config as cfg
from bnbc.agent.prompts import render_spec_card_compact
from bnbc.llm import TokenMeter, call_llm, system, user
from bnbc.agent.state import get_candidate

logger = logging.getLogger("bnbc.agent.conformance")

MAX_SAMPLES = 3


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------

class ConformanceIssue(BaseModel):
    condition: str = Field(
        "", description="Spec-card condition id the issue concerns (required for blockers)"
    )
    severity: Literal["blocker", "advisory"] = "advisory"
    note: str = ""


class ConformanceReview(BaseModel):
    issues: list[ConformanceIssue] = Field(default_factory=list)
    ok: bool = Field(..., description="True if the code faithfully implements the spec card")


REVIEWER_SYSTEM_PROMPT = """\
You are a conformance reviewer for generated building-code checkers. You are
NOT the acceptance authority — deterministic fixtures already gate acceptance.
Your single job: compare the checker CODE against the SPEC CARD and flag spec
conditions that are missing, incorrectly implemented, or that contradict an
adjudicated convention.

Rules:
- severity="blocker" ONLY for a spec condition that is not implemented or is
  implemented incorrectly; set `condition` to that condition's exact id.
- Everything else (style, performance, robustness suggestions) is
  severity="advisory".
- Judge from the code and the summarized execution evidence. Do not speculate
  about IFC data you cannot see.
- ok=true when every spec condition is faithfully implemented.
"""


# ---------------------------------------------------------------------------
# Result summarization (context diet — never raw json.dumps of results)
# ---------------------------------------------------------------------------

def summarize_results(candidate: dict[str, Any], max_samples: int = MAX_SAMPLES) -> str:
    """Per-condition counts + <=max_samples sample locations, plus gate summary."""
    lines: list[str] = []

    # Real-model runs.
    lines.append("### Real model runs")
    per_condition: dict[str, dict[str, Any]] = {}
    for res in candidate.get("real_results", []) or []:
        fname = res.get("file_name", "?")
        if not res.get("success") or not isinstance(res.get("result"), dict):
            err = str(res.get("error", ""))[:200]
            lines.append(f"- {fname}: EXECUTION ERROR — {err}")
            continue
        result = res["result"]
        lines.append(f"- {fname}: verdict={result.get('verdict')}")
        for cond_id, cov in (result.get("checked_summary") or {}).items():
            agg = per_condition.setdefault(
                cond_id, {"checked": 0, "skipped": 0, "violations": 0, "samples": []}
            )
            agg["checked"] += int(cov.get("elements_checked", 0) or 0)
            agg["skipped"] += int(cov.get("elements_skipped", 0) or 0)
        for viol in result.get("violations") or []:
            cond_id = viol.get("condition", "?")
            agg = per_condition.setdefault(
                cond_id, {"checked": 0, "skipped": 0, "violations": 0, "samples": []}
            )
            locations = viol.get("locations") or []
            agg["violations"] += len(locations)
            for loc in locations:
                if len(agg["samples"]) < max_samples:
                    agg["samples"].append(
                        f"{fname}: {loc.get('element', '?')} — {loc.get('measured', '')}"
                    )

    if per_condition:
        lines.append("### Per-condition aggregate")
        for cond_id, agg in sorted(per_condition.items()):
            lines.append(
                f"- [{cond_id}] checked={agg['checked']} skipped={agg['skipped']} "
                f"violation_locations={agg['violations']}"
            )
            for sample in agg["samples"]:
                lines.append(f"    sample: {sample}")

    # Fixture gate outcomes.
    acceptance = candidate.get("acceptance") or {}
    outcomes = acceptance.get("outcomes") or []
    if outcomes:
        ok_n = sum(1 for o in outcomes if o.get("ok"))
        lines.append(f"### Fixture gate: {ok_n}/{len(outcomes)} fixtures ok")
        shown = 0
        for o in outcomes:
            if not o.get("ok") and shown < max_samples:
                lines.append(
                    f"- FAILED {o.get('fixture_id')}: expected={o.get('expected_verdict')} "
                    f"actual={o.get('actual_verdict')} missed={o.get('conditions_missed')}"
                )
                shown += 1
    for err in (acceptance.get("schema_errors") or [])[:max_samples]:
        lines.append(f"- schema error: {str(err)[:200]}")
    for err in (acceptance.get("static_errors") or [])[:max_samples]:
        lines.append(f"- static error: {str(err)[:200]}")

    return "\n".join(lines) if lines else "(no execution evidence)"


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def conformance_node(state: dict) -> dict:
    """One bounded structured ADVISORY review of the gate-accepted candidate.

    Best-effort by design: any failure here (provider down, budget exhausted,
    parse error) is logged and recorded — it must NEVER prevent a gate-accepted
    checker from being stored, so this node swallows every exception instead
    of relying on the graph guard.
    """
    if not cfg.ENABLE_CONFORMANCE_REVIEW:
        return {"status": "conformance_skipped"}

    selected = get_candidate(state)
    if selected is None:
        return {
            "conformance": {"ok": None, "issues": [], "actionable_blockers": [], "advisories": []},
            "status": "conformance_skipped",
        }

    meter = TokenMeter.from_state(state.get("token_usage"))
    spec_card = state.get("spec_card") or {}
    condition_ids = {c.get("id") for c in spec_card.get("conditions", []) or []}

    content = "\n\n".join(
        [
            render_spec_card_compact(spec_card),
            "## Accepted checker code\n```python\n" + selected.get("code", "") + "\n```",
            "## Summarized execution results\n" + summarize_results(selected),
        ]
    )
    try:
        review: ConformanceReview = await call_llm(
            cfg.REVIEWER_MODEL_NAME,
            [system(REVIEWER_SYSTEM_PROMPT), user(content)],
            meter,
            "conformance",
            structured=ConformanceReview,
        )
    except Exception as exc:
        logger.warning(
            "Rule %s: advisory conformance review failed (never blocks storage): %s",
            state.get("rule_id"), exc,
        )
        return {
            "conformance": {
                "ok": None, "issues": [], "actionable_blockers": [], "advisories": [],
                "error": str(exc),
            },
            "token_usage": meter.to_state(),
            "status": "conformance_error",
        }

    actionable: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    for issue in review.issues:
        dumped = issue.model_dump()
        if issue.severity == "blocker" and issue.condition in condition_ids:
            actionable.append(dumped)
        else:
            if issue.severity == "blocker":
                logger.warning(
                    "Blocker without a valid spec condition id (%r) demoted to advisory: %s",
                    issue.condition, issue.note,
                )
            else:
                logger.info("Conformance advisory [%s]: %s", issue.condition, issue.note)
            advisories.append(dumped)

    ok = review.ok and not actionable
    logger.info(
        "Rule %s: conformance review ok=%s (%d actionable blocker(s), %d advisory(ies))",
        state.get("rule_id"), ok, len(actionable), len(advisories),
    )
    return {
        "conformance": {
            "ok": ok,
            "issues": [i.model_dump() for i in review.issues],
            "actionable_blockers": actionable,
            "advisories": advisories,
        },
        "token_usage": meter.to_state(),
        "status": "conformance_done",
    }
