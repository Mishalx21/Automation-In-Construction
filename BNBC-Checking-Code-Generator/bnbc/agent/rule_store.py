"""Filesystem rule store — the pipeline's only persistence.

:class:`RuleStore` writes per-rule artifacts under ``rules/<rule_id>/``:
``checker.py``, ``acceptance.json``, ``prerequisites.json``,
``provenance.json`` on acceptance, ``rejection.json`` (plus a ``partial/``
work-product) on rejection. The same directory holds the rule's inputs
(``rule.json``) and its interpretation contract (``spec_card.yaml``), so one
directory is the complete, diffable record of a rule.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from bnbc import config as cfg
from bnbc.contracts import PrerequisitesManifest, SpecCard
from bnbc.agent.state import get_candidate

logger = logging.getLogger("bnbc.agent.rule_store")

CHECKER_FILE = "checker.py"
ACCEPTANCE_FILE = "acceptance.json"
PREREQUISITES_FILE = "prerequisites.json"
PROVENANCE_FILE = "provenance.json"
REJECTION_FILE = "rejection.json"


class RuleStore:
    """Filesystem store: rules/<rule_id>/{checker.py, acceptance.json, ...}."""

    def __init__(self, artifacts_dir: Path | str | None = None):
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir is not None else cfg.ARTIFACTS_DIR

    def rule_dir(self, rule_id: str) -> Path:
        return self.artifacts_dir / rule_id

    def save(
        self,
        rule_id: str,
        code: str,
        acceptance: dict[str, Any],
        prerequisites: dict[str, Any],
        provenance: dict[str, Any],
    ) -> dict[str, Path]:
        """Write all four artifacts; returns their paths."""
        rdir = self.rule_dir(rule_id)
        rdir.mkdir(parents=True, exist_ok=True)
        paths = {
            "checker": rdir / CHECKER_FILE,
            "acceptance": rdir / ACCEPTANCE_FILE,
            "prerequisites": rdir / PREREQUISITES_FILE,
            "provenance": rdir / PROVENANCE_FILE,
        }
        paths["checker"].write_text(code, encoding="utf-8")
        for key, payload in (
            ("acceptance", acceptance),
            ("prerequisites", prerequisites),
            ("provenance", provenance),
        ):
            paths[key].write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        logger.info("Rule %s: stored artifacts in %s", rule_id, rdir)
        return paths

    def load_accepted(self) -> list[dict[str, Any]]:
        """All rules with a stored checker whose acceptance report says accepted.

        Returns ``[{rule_id, code, acceptance, spec_card|None}]`` — the
        retrieval index source.
        """
        entries: list[dict[str, Any]] = []
        if not self.artifacts_dir.exists():
            return entries
        for rdir in sorted(p for p in self.artifacts_dir.iterdir() if p.is_dir() and p.name != "fixtures"):
            checker = rdir / CHECKER_FILE
            acceptance_path = rdir / ACCEPTANCE_FILE
            if not checker.exists() or not acceptance_path.exists():
                continue
            try:
                acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Skipping %s: bad acceptance.json (%s)", rdir.name, exc)
                continue
            if not acceptance.get("accepted"):
                continue
            spec_card = None
            spec_path = rdir / "spec_card.yaml"
            if spec_path.exists():
                try:
                    spec_card = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Bad spec_card.yaml in %s: %s", rdir.name, exc)
            entries.append(
                {
                    "rule_id": rdir.name,
                    "code": checker.read_text(encoding="utf-8"),
                    "acceptance": acceptance,
                    "spec_card": spec_card,
                }
            )
        return entries


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

def build_provenance(state: dict, selected: dict[str, Any]) -> dict[str, Any]:
    acceptance = selected.get("acceptance") or {}
    outcomes = acceptance.get("outcomes") or []
    spec_card = state.get("spec_card") or {}
    return {
        "rule_id": state["rule_id"],
        "candidate_id": selected["candidate_id"],
        "model_name": selected.get("model_name", ""),
        "llm_provider": cfg.LLM_PROVIDER,
        "spec_model": cfg.SPEC_MODEL_NAME,
        "drafter_model": cfg.DRAFTER_MODEL,
        "reviewer_model": cfg.REVIEWER_MODEL_NAME,
        "spec_card_version": spec_card.get("version", 1),
        "spec_card_source": state.get("spec_card_source", ""),
        "agent_turns": int(selected.get("agent_turns") or 0),
        "agent_inspects": int(selected.get("agent_inspects") or 0),
        "spec_revisions": int(state.get("spec_revisions") or 0),
        "token_usage": state.get("token_usage") or {},
        "fixtures": {
            "total": len(outcomes),
            "passed": sum(1 for o in outcomes if o.get("ok")),
            "kill_rate": selected.get("kill_rate", 0.0),
        },
        "conformance": {
            "ok": (state.get("conformance") or {}).get("ok"),
            # Unresolved blockers on a gate-accepted candidate are FLAGS, not
            # vetoes (D4) — persisted here for offline review.
            "unresolved_blockers": (state.get("conformance") or {}).get(
                "actionable_blockers", []
            ),
            "advisories": (state.get("conformance") or {}).get("advisories", []),
        },
        "stored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


async def store_node(state: dict) -> dict:
    """Persist the accepted checker, its manifests, and its provenance."""
    selected = get_candidate(state)
    if selected is None:
        return {
            "status": "store_failed",
            "escalation_reason": "store node reached without a selected candidate",
        }

    rule_id = state["rule_id"]
    spec_card = SpecCard.model_validate(state.get("spec_card") or {"rule_id": rule_id})
    prerequisites = PrerequisitesManifest(
        rule_id=rule_id, prerequisites=spec_card.prerequisites
    )

    store = RuleStore(Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR))
    store.save(
        rule_id=rule_id,
        code=selected.get("code", ""),
        acceptance=selected.get("acceptance") or {},
        prerequisites=prerequisites.model_dump(mode="json"),
        provenance=build_provenance(state, selected),
    )
    # A previous rejection (and any partial evidence) is superseded by this
    # acceptance.
    rejection = store.rule_dir(rule_id) / REJECTION_FILE
    if rejection.exists():
        rejection.unlink()
    partial = store.rule_dir(rule_id) / "partial"
    if partial.exists():
        import shutil

        shutil.rmtree(partial, ignore_errors=True)
    return {"status": "stored"}


def _verified_conditions(cand: dict[str, Any], state: dict) -> list[str]:
    """Conditions whose every naming fixture outcome is ok (and >=1 exists).

    A fixture's conditions come from the manifest (FixtureSpec.expected_conditions);
    outcomes only carry hit/missed lists, which serve as the fallback.
    """
    manifest = state.get("fixture_manifest") or {}
    cond_by_fixture = {
        f.get("fixture_id"): list(f.get("expected_conditions") or [])
        for f in manifest.get("fixtures", []) or []
    }
    outcomes = (cand.get("acceptance") or {}).get("outcomes") or []
    by_cond: dict[str, list[bool]] = {}
    for o in outcomes:
        cids = cond_by_fixture.get(o.get("fixture_id")) or (
            list(o.get("conditions_hit") or []) + list(o.get("conditions_missed") or [])
        )
        for cid in cids:
            by_cond.setdefault(cid, []).append(bool(o.get("ok")))
    return sorted(c for c, oks in by_cond.items() if oks and all(oks))


def _write_draft_artifact(rdir: Path, state: dict, cand: dict[str, Any]) -> None:
    """Every rejection with a candidate delivers a runnable artifact.

    ``partial/checker.draft.py`` is the best candidate as-is (possibly wrong,
    never gate-accepted) plus a machine-readable ``draft_summary.json`` with
    the failure evidence. A rejected run must never leave the user with
    nothing: the draft is a starting point for manual completion and the
    summary says exactly what the gate still disputes.
    """
    if not cand.get("code"):
        return
    pdir = rdir / "partial"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "checker.draft.py").write_text(cand["code"], encoding="utf-8")
    outcomes = (cand.get("acceptance") or {}).get("outcomes") or []
    (pdir / "draft_summary.json").write_text(
        json.dumps({
            "rule_id": state["rule_id"],
            "needs_human_intervention": True,
            "note": (
                "NOT gate-accepted — best candidate of the whole run, "
                "stored on rejection as the human starting point. Review the "
                "failure evidence below before any use."
            ),
            "candidate_id": cand.get("candidate_id"),
            "model_name": cand.get("model_name"),
            "fault_class": cand.get("fault_class", ""),
            "kill_rate": cand.get("kill_rate", 0.0),
            "fixtures_ok": sum(1 for o in outcomes if o.get("ok")),
            "fixtures_total": len(outcomes),
            "schema_errors": cand.get("schema_errors", []),
            "static_errors": cand.get("static_errors", []),
            "repair_rounds": int(state.get("repair_rounds") or 0),
            "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logger.info("Rule %s: best-effort draft stored in %s", state["rule_id"], pdir)


def _write_partial_evidence(rdir: Path, state: dict, cand: dict[str, Any]) -> list[str]:
    """Salvage verified work on terminal rejection — WITHOUT weakening the gate.

    When some conditions passed every one of their fixtures (a rejection is
    usually one hard condition blocking several provable ones), the candidate
    and its per-condition evidence are stored under ``partial/``. This is
    diagnostic work-product for humans: ``accepted`` stays False, the
    retrieval index never sees it, and the gate remains the sole acceptance
    authority.
    """
    verified = _verified_conditions(cand, state)
    if not (verified and cand.get("code")
            and not cand.get("schema_errors") and not cand.get("static_errors")):
        return []
    all_conditions = sorted(
        c.get("id", "") for c in (state.get("spec_card") or {}).get("conditions", []) or []
    )
    pdir = rdir / "partial"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "checker.py").write_text(cand.get("code", ""), encoding="utf-8")
    (pdir / "partial_acceptance.json").write_text(
        json.dumps({
            "rule_id": state["rule_id"],
            "note": (
                "NOT gate-accepted. The conditions below passed ALL their "
                "fixtures; the rest did not — treat their verdicts as unverified."
            ),
            "verified_conditions": verified,
            "unverified_conditions": [c for c in all_conditions if c and c not in verified],
            "kill_rate": cand.get("kill_rate", 0.0),
            "acceptance": cand.get("acceptance") or {},
            "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logger.info("Rule %s: partial evidence stored (%d/%d conditions verified) in %s",
                state["rule_id"], len(verified), len(all_conditions), pdir)
    return verified


async def reject_node(state: dict) -> dict:
    """Terminal reject: persist the structured reason, hand off to a human.

    The agent is a full-automation attempt with an honest escape hatch: some
    rules are not (yet) feasible for it, and the correct terminal behaviour is
    to keep the BEST candidate seen across the whole run (not the last
    rewrite — iterations oscillate) and tag the rule as needing human
    intervention. rejection.json carries everything needed to finish the job
    manually: the best draft, per-fixture evidence, verified conditions, spec
    provenance, token usage.
    """
    from bnbc.agent.execution import candidate_score

    rule_id = state["rule_id"]
    cand = state.get("candidate") or {}
    best = state.get("best_candidate") or {}
    candidate_is_best_of_run = False
    if best.get("code") and candidate_score(best) > candidate_score(cand):
        logger.info(
            "Rule %s: final candidate (%s) is worse than the best of the run "
            "(%s) — the rejection keeps the best",
            rule_id, candidate_score(cand), candidate_score(best),
        )
        cand = best
        candidate_is_best_of_run = True
    reason = state.get("escalation_reason")
    if not reason:
        if cand:
            reason = (
                f"fixture gate rejected the candidate "
                f"(fault_class={cand.get('fault_class') or '?'}) after "
                f"{int(cand.get('agent_turns') or 0)} agent turn(s)"
            )
        else:
            reason = f"rejected at status={state.get('status', '?')}"
    payload = {
        "rule_id": rule_id,
        "reason": reason,
        # Full automation was attempted and exhausted its bounded budgets —
        # this rule now needs a human. The best draft (if any) is under
        # partial/ as the starting point.
        "needs_human_intervention": True,
        "candidate_is_best_of_run": candidate_is_best_of_run,
        "status_at_rejection": state.get("status", ""),
        "spec_card_source": state.get("spec_card_source", ""),
        "adjudication_open_items": state.get("adjudication_open_items", []),
        "candidate": {
            "candidate_id": cand.get("candidate_id"),
            "model_name": cand.get("model_name"),
            "accepted": bool(cand.get("accepted")),
            "fault_class": cand.get("fault_class", ""),
            "kill_rate": cand.get("kill_rate", 0.0),
            "failed_fixtures": [
                o for o in (cand.get("acceptance") or {}).get("outcomes", [])
                if not o.get("ok")
            ],
            "schema_errors": cand.get("schema_errors", []),
            "static_errors": cand.get("static_errors", []),
            "extraction_errors": cand.get("extraction_errors", []),
            # Full candidate code — without it a rejection cannot be
            # diagnosed offline (the code lives nowhere else).
            "code": cand.get("code", ""),
        } if cand else None,
        "agent_turns": int(cand.get("agent_turns") or 0),
        "agent_inspects": int(cand.get("agent_inspects") or 0),
        "spec_revisions": int(state.get("spec_revisions") or 0),
        "token_usage": state.get("token_usage") or {},
        "rejected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    rdir = RuleStore(Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR)).rule_dir(rule_id)
    rdir.mkdir(parents=True, exist_ok=True)
    if cand:
        _write_draft_artifact(rdir, state, cand)
    payload["verified_conditions"] = _write_partial_evidence(rdir, state, cand) if cand else []
    path = rdir / REJECTION_FILE
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    logger.warning("Rule %s REJECTED: %s (details: %s)", rule_id, reason, path)
    return {"status": "rejected"}
