"""
Shared contracts (FIV — Fixture-Injected Verification).

The single source of truth for the data shapes exchanged between the
spec-card stage, the drafter, the fixture engine, and the acceptance gate.
Every other module imports from here; nothing here imports from the rest of
the package.

Verdict semantics (four-valued):
  pass            — rule checked, all checked elements comply
  fail            — rule checked, at least one violation found
  unknown         — required data missing/unparseable for at least one
                    condition; compliance cannot be determined
  not_applicable  — no elements in scope for this rule in this model

A checker that examined zero elements MUST NOT return "pass"; it must
return "not_applicable" (no elements of the required type) or "unknown"
(elements exist but required data is missing).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Verdicts (check_rule output schema v2)
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ViolationLocation(BaseModel):
    element: str = Field(..., description='Element label, format "Name (GlobalId)"')
    storey: str = Field("Unknown level")
    measured: str = Field(..., description='Measured vs required, e.g. "ext=45.0 mm, required=76.0 mm"')


class Violation(BaseModel):
    condition: str = Field(..., description="Machine-readable condition id from the spec card")
    description: str
    rule_ref: str
    threshold: str
    locations: list[ViolationLocation] = Field(default_factory=list)


class UnknownReason(BaseModel):
    condition: str
    missing: str = Field(..., description="What data was missing, e.g. 'NominalDiameter on 12 bars'")
    affected_elements: int = 0


class ConditionCoverage(BaseModel):
    elements_checked: int = 0
    elements_skipped: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)


class CheckResultV2(BaseModel):
    """The contract every generated ``check_rule(model)`` must return (as a dict)."""

    verdict: Verdict
    violations: list[Violation] = Field(default_factory=list)
    violation_count: int = 0
    unknown_reasons: list[UnknownReason] = Field(default_factory=list)
    checked_summary: dict[str, ConditionCoverage] = Field(
        default_factory=dict, description="Per-condition coverage accounting"
    )
    summary: str = ""

    @model_validator(mode="after")
    def _consistency(self) -> "CheckResultV2":
        total_locations = sum(len(v.locations) for v in self.violations)
        if self.violation_count != total_locations:
            raise ValueError(
                f"violation_count={self.violation_count} but sum of locations={total_locations}"
            )
        if self.verdict == Verdict.FAIL and not self.violations:
            raise ValueError("verdict=fail but violations list is empty")
        if self.verdict in (Verdict.PASS, Verdict.NOT_APPLICABLE) and self.violations:
            raise ValueError(f"verdict={self.verdict.value} but violations present")
        if self.verdict == Verdict.UNKNOWN and not self.unknown_reasons:
            raise ValueError("verdict=unknown requires at least one unknown_reason")
        if self.verdict == Verdict.PASS:
            checked = sum(c.elements_checked for c in self.checked_summary.values())
            if checked == 0:
                raise ValueError(
                    "verdict=pass with zero elements checked — must be not_applicable or unknown"
                )
        return self


def validate_check_result(raw: Any) -> list[str]:
    """Validate a raw dict against CheckResultV2. Returns a list of error strings (empty if valid)."""
    if not isinstance(raw, dict):
        return [f"result is not a dict (got {type(raw).__name__})"]
    try:
        CheckResultV2.model_validate(raw)
        return []
    except Exception as exc:  # pydantic ValidationError formats multi-error nicely
        return [str(exc)]


# ---------------------------------------------------------------------------
# Data prerequisites manifest (IDS-aligned)
# ---------------------------------------------------------------------------

class DataPrerequisite(BaseModel):
    condition: str = Field(..., description="Condition id this prerequisite serves")
    entity: str = Field(..., description='IFC entity type, e.g. "IfcReinforcingBar"')
    requirement: str = Field(
        ...,
        description=(
            'What must be present/derivable, e.g. "NominalDiameter attribute or '
            'diameter-bearing pset/Name" or "parseable directrix geometry"'
        ),
    )
    on_missing: Verdict = Field(
        Verdict.UNKNOWN, description="Verdict contribution when this prerequisite is unmet"
    )


class PrerequisitesManifest(BaseModel):
    rule_id: str
    prerequisites: list[DataPrerequisite] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Spec Card (rule interpretation contract, human-adjudicated)
# ---------------------------------------------------------------------------

class SpecThreshold(BaseModel):
    parameter: str
    operator: Literal["<", "<=", ">", ">=", "==", "!="]
    value: str
    unit: str = ""


class SpecCondition(BaseModel):
    """One normative condition, RASE-style."""

    id: str = Field(..., description="Machine-readable id, e.g. 'hook180_extension_min'")
    requirement: str = Field(..., description="What must hold (normative requirement)")
    applicability: str = Field("", description="Which elements/situations the condition applies to")
    exceptions: list[str] = Field(default_factory=list)
    formula: str = ""
    thresholds: list[SpecThreshold] = Field(default_factory=list)
    required_data: list[str] = Field(default_factory=list, description="IFC data needed to evaluate")


class MeasurementConvention(BaseModel):
    """An interpretation decision that the rule text leaves ambiguous."""

    topic: str = Field(..., description="e.g. 'hook extension measurement datum'")
    decision: str = Field(..., description="The adjudicated convention")
    rationale: str = ""
    status: Literal["adjudicated", "proposed", "open"] = "proposed"
    adjudicated_by: str = ""
    gates_applicability: bool = Field(
        False,
        description=(
            "True when this convention restricts which elements/models the rule "
            "applies to (an applicability predicate, not a measurement datum)"
        ),
    )
    fixture_discharge: str = Field(
        "",
        description=(
            "Required when gates_applicability: how the card's own fail/pass "
            "fixtures satisfy the applicability predicate. A convention must "
            "never render the card's own fixtures not_applicable."
        ),
    )


class FixtureStep(BaseModel):
    """One operator application inside a fixture (steps run in order on the
    same model, so later steps may target elements earlier steps inserted)."""

    operator: str = Field(..., description="bnbc.fixtures operator name")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Operator keyword params; lengths in mm, angles in deg"
    )
    target: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            'Target element selector, e.g. {"ifc_class": "IfcReinforcingBar", '
            '"name_contains": "...", "index": 0, "all": false}'
        ),
    )


class FixtureSketch(BaseModel):
    """One machine-executable fixture plan item (input to the fixture planner).

    ``operator``/``params``/``target`` must use the ``bnbc.fixtures`` operator
    vocabulary — the planner validates them and refuses free-text plans, so
    fixture materialisation stays deterministic. A compound perturbation uses
    ``steps`` instead (applied in order to the SAME model); sketches are
    always independent of each other — one sketch, one fixture file.
    """

    condition: str
    perturbation: str = Field(..., description="Human-readable description of the perturbation")
    expected_verdict: Verdict = Verdict.FAIL
    operator: str = Field("", description="bnbc.fixtures operator name, e.g. 'shorten_hook_tail'")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Operator keyword params; lengths in mm, angles in deg"
    )
    target: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            'Target element selector, e.g. {"ifc_class": "IfcReinforcingBar", '
            '"name_contains": "...", "with_hook_angle_deg": 180, "index": 0, "all": false}'
        ),
    )
    steps: list[FixtureStep] = Field(
        default_factory=list,
        description=(
            "Compound perturbation: operator applications run IN ORDER on the "
            "same model (mutually exclusive with operator/params/target)"
        ),
    )
    base_model: str = Field(
        "", description='Base IFC file name; "" = first real model, "__synthetic__" = micro-model scaffold'
    )
    boundary: bool = Field(
        False, description="True when the perturbation sits at threshold±epsilon (boundary case)"
    )


class SpecCard(BaseModel):
    rule_id: str
    rule_title: str = ""
    source_text: str = Field("", description="Verbatim rule text")
    scope_note: str = Field("", description="What parts of the rule this card covers / defers")
    conditions: list[SpecCondition] = Field(default_factory=list)
    conventions: list[MeasurementConvention] = Field(default_factory=list)
    prerequisites: list[DataPrerequisite] = Field(default_factory=list)
    fixture_sketches: list[FixtureSketch] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list, description="Open questions blocking adjudication")
    version: int = 1

    @property
    def is_adjudicated(self) -> bool:
        open_conventions = [c for c in self.conventions if c.status != "adjudicated"]
        return not open_conventions and not self.ambiguities


# ---------------------------------------------------------------------------
# Fixtures (manufactured ground truth)
# ---------------------------------------------------------------------------

class SelfVerification(BaseModel):
    """Independent measurement proving the perturbation did what it claims.

    MUST be produced by measurement code independent of ifc_helpers.
    """

    measured: str = Field(..., description="What was measured post-write, e.g. 'tail=40.0 mm'")
    expected: str = Field(..., description="What the operator intended")
    ok: bool


class FixtureSpec(BaseModel):
    fixture_id: str
    rule_id: str
    base_model: str = Field(..., description="File name of the unperturbed IFC")
    operator: str = Field(..., description="First operator name (display/file naming)")
    params: dict[str, Any] = Field(default_factory=dict)
    steps: list[FixtureStep] = Field(
        default_factory=list,
        description="Normalised operator chain (the planner always fills this)",
    )
    content_hash: str = Field(
        "", description="Hash of (base_model, steps) — the fixture reuse key"
    )
    source_sketch_indices: list[int] = Field(
        default_factory=list,
        description=(
            "Spec-card fixture_sketches indices this fixture materialises "
            "(several after cross-condition dedup) — build failures map back "
            "to sketches for targeted plan repair"
        ),
    )
    file_name: str = Field(..., description="File name of the produced fixture IFC")
    expected_verdict: Verdict
    expected_conditions: list[str] = Field(
        default_factory=list, description="Condition ids that must appear in violations (if fail)"
    )
    expected_elements: list[str] = Field(
        default_factory=list, description="GlobalIds that must appear in violation locations (if fail)"
    )
    self_verification: Optional[SelfVerification] = None


class FixtureManifest(BaseModel):
    rule_id: str
    fixtures: list[FixtureSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------

class FixtureOutcome(BaseModel):
    fixture_id: str
    expected_verdict: Verdict
    actual_verdict: Optional[Verdict] = None
    conditions_hit: list[str] = Field(default_factory=list)
    conditions_missed: list[str] = Field(default_factory=list)
    elements_hit: list[str] = Field(default_factory=list)
    elements_missed: list[str] = Field(default_factory=list)
    execution_error: str = ""
    checker_summary: str = Field(
        "",
        description=(
            "What the checker itself reported (summary + per-condition "
            "coverage + unknown reasons) — paired with the fixture's "
            "independently-measured ground truth it forms the drafter's "
            "probe report"
        ),
    )
    ok: bool = False


class AcceptanceReport(BaseModel):
    rule_id: str
    candidate_id: str = ""
    outcomes: list[FixtureOutcome] = Field(default_factory=list)
    schema_errors: list[str] = Field(default_factory=list)
    static_errors: list[str] = Field(default_factory=list)
    runtime_ms_max: int = 0
    accepted: bool = False

    @property
    def kill_rate(self) -> float:
        must_fail = [o for o in self.outcomes if o.expected_verdict == Verdict.FAIL]
        if not must_fail:
            return 1.0
        return sum(1 for o in must_fail if o.ok) / len(must_fail)
