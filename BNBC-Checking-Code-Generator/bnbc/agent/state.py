"""State schema for the agent graph.

Everything in the state is a plain JSON-serialisable value (dicts, lists,
strings, ints). Pydantic contract objects (SpecCard, FixtureManifest,
AcceptanceReport, ...) are stored as ``model_dump(mode="json")`` dicts and
re-validated at the point of use.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # ---- Rule inputs (set once by ingest) ----
    rule_id: str
    rule_title: str
    #: The curated normative statement from rules/<rule_id>/rule.json.
    rule_statement: str
    #: What the rule deliberately does not cover.
    rule_scope_note: str
    #: BNBC clause ids this rule checks.
    source_clauses: list[str]
    #: Verbatim BNBC context resolved from the rulebook and rendered for
    #: prompts: source clauses, cited tables/figures/equations, defined terms.
    rule_context: str
    #: Citations the rulebook could not satisfy — surfaced, never invented.
    unresolved_references: list[str]
    #: Directories, as strings so the state stays JSON-serialisable.
    #: rules_dir/rulebook_dir are read-only inputs; artifacts_dir is written to.
    rules_dir: str
    rulebook_dir: str
    artifacts_dir: str
    #: Absolute paths of the real IFC corpus (resolved by ingest).
    base_models: list[str]

    # ---- Spec card / adjudication ----
    spec_card: dict[str, Any]                 # contracts.SpecCard as dict
    #: "disk" | "generated" | "sketch_repaired" | "regenerated" | "revised"
    spec_card_source: str
    adjudication_status: str                  # "adjudicated" | "model_resolved"
    adjudication_open_items: list[str]

    # ---- Fixtures ----
    fixture_manifest: dict[str, Any]          # contracts.FixtureManifest as dict
    plan_errors: str                          # planner defect list (spec-card retry input)
    plan_defects: list[dict[str, Any]]        # structured defects [{sketch_index, message}]
    spec_plan_regens: int                     # plan-invalid regenerations (max cfg.MAX_PLAN_REGENS)
    #: Outer-loop bookkeeping: gate-evidence spec revisions used (max
    #: cfg.MAX_SPEC_REVISIONS).
    spec_revisions: int

    # ---- Retrieval (drafter grounding) ----
    exemplars: list[dict[str, Any]]           # [{rule_id, spec_summary, code, score}]
    helper_reference: str                     # auto-generated ifc_helpers doc
    model_profile: str                        # census/digest-based base-model profile

    # ---- Candidate ----
    #: The agent session's chosen module plus its gate results:
    #:   code, model_name, candidate_id, extraction_errors,
    #:   agent_turns / agent_inspects (session cost),
    #:   real_results, verdict_vector, acceptance, kill_rate, runtime_ms_max,
    #:   static_errors / schema_errors, accepted (bool),
    #:   fault_class ("code" | "oracle", set by execute after a failed gate).
    candidate: dict[str, Any]
    #: Best-scoring candidate seen across the run (elitism): a terminal
    #: rejection hands IT to the human, not a later regression. Snapshot of
    #: the fields in execution._BEST_FIELDS.
    best_candidate: dict[str, Any]

    # ---- Conformance review (advisory, post-acceptance) ----
    #: {ok, issues: [...], actionable_blockers: [...], advisories: [...]}
    conformance: dict[str, Any]

    # ---- Accounting / control ----
    #: {"per_node": {node: {input_tokens, output_tokens, calls}}, "total": int, ...}
    token_usage: dict[str, Any]
    status: str
    escalation_reason: str


def get_candidate(state: dict) -> dict[str, Any] | None:
    """The run's single candidate, or None before drafting."""
    cand = state.get("candidate")
    return cand if cand else None
