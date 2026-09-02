"""Thin facade over the ``bnbc.fixtures`` engine.

The agent depends only on this module; ``bnbc.fixtures`` is imported lazily
inside the functions, so importing the agent (and running most of its tests)
never pays for the ifcopenshell chain. Tests monkeypatch these two functions
to run the whole graph without touching real IFC files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bnbc.contracts import AcceptanceReport, FixtureManifest, SpecCard

logger = logging.getLogger("bnbc.agent.fixtures_facade")


def _as_manifest(obj: Any, rule_id: str) -> FixtureManifest:
    if isinstance(obj, FixtureManifest):
        return obj
    if isinstance(obj, dict):
        return FixtureManifest.model_validate(obj)
    if isinstance(obj, list):  # bare list of FixtureSpec-likes
        return FixtureManifest.model_validate({"rule_id": rule_id, "fixtures": obj})
    raise TypeError(f"cannot coerce {type(obj).__name__} to FixtureManifest")


def ensure_fixtures(spec_card: SpecCard, base_models: list[str]) -> FixtureManifest:
    """Plan + materialise the fixture set for a spec card.

    Wraps ``bnbc.fixtures.planner.plan_fixtures`` and
    ``bnbc.fixtures.manifest.build_manifest``; normalises the result to a
    validated :class:`FixtureManifest`.
    """
    from bnbc.fixtures import manifest as manifest_mod  # lazy (heavy: ifcopenshell)
    from bnbc.fixtures import planner as planner_mod
    from bnbc.fixtures.errors import FixtureBuildError, TargetNotFoundError, PlanningError

    try:
        planned = planner_mod.plan_fixtures(spec_card, base_models)
        base_dir = Path(base_models[0]).parent if base_models else None
        built = manifest_mod.build_manifest(spec_card, planned, base_dir=base_dir)
    except FixtureBuildError as exc:
        # A per-fixture build/self-verification failure maps back to the
        # sketches that authored it — a plan defect with feedback, never a
        # terminal engine crash (both live rejections to date died here).
        raise PlanningError(
            f"fixture build failed: {exc}. Fix or replace the responsible "
            "sketch(es); the operator chain, params, or targets are "
            "inconsistent with what the base model / earlier steps provide.",
            defects=[
                {"sketch_index": i, "message": str(exc)}
                for i in (exc.sketch_indices or [None])
            ],
        ) from exc
    except TargetNotFoundError as exc:
        raise PlanningError(
            f"Target not found during fixture generation: {exc}. "
            "Please check if the element exists in the base model or use a different target selector."
        )
    manifest = _as_manifest(built, spec_card.rule_id)
    logger.info(
        "Rule %s: fixture manifest ready (%d fixtures)",
        spec_card.rule_id, len(manifest.fixtures),
    )
    return manifest


async def fixture_plan_node(state: dict) -> dict:
    """Plan + build (or reuse) the rule's fixture set from the spec card.

    A ``PlanningError`` (invalid sketches, and — via ``FixtureBuildError``
    mapping — per-fixture build/self-verification failures) is returned as
    ``plan_invalid`` so the graph can repair/regenerate the spec card within
    the bounded plan-regen budget. Only infrastructure-level fixture failures
    (e.g. a missing base-model file) still propagate to the guard and
    terminate as ``rejected`` — never silently skipped.
    """
    from bnbc.fixtures.errors import PlanningError  # lazy (heavy import chain)

    card = SpecCard.model_validate(state["spec_card"])
    base_models = list(state.get("base_models") or [])
    try:
        manifest = ensure_fixtures(card, base_models)
    except PlanningError as exc:
        logger.warning("Rule %s: fixture plan invalid:\n%s", card.rule_id, exc)
        return {
            "status": "plan_invalid",
            "plan_errors": str(exc),
            # Structured defect list (sketch_index per record) — enables
            # sketch-level repair instead of whole-card regeneration.
            "plan_defects": list(getattr(exc, "defects", []) or []),
            "escalation_reason": f"fixture plan invalid: {exc}",
        }
    return {
        "fixture_manifest": manifest.model_dump(mode="json"),
        # The plan succeeded: clear retry inputs AND any stale escalation
        # reason a recovered plan_invalid left behind (observed live: a
        # rejection.json blamed a plan defect the run had already fixed).
        "plan_errors": "",
        "plan_defects": [],
        "escalation_reason": "",
        "status": "fixtures_ready",
    }


def run_acceptance_gate(
    rule_id: str,
    code: str,
    manifest: FixtureManifest,
    real_models: list[str | Path],
    timeout_s: int,
) -> AcceptanceReport:
    """Run the deterministic acceptance gate (``bnbc.fixtures.gate.run_gate``)."""
    from bnbc.fixtures import gate as gate_mod  # lazy (parallel dev)

    report = gate_mod.run_gate(rule_id, code, manifest, [str(m) for m in real_models], timeout_s)
    if isinstance(report, AcceptanceReport):
        return report
    return AcceptanceReport.model_validate(report)
