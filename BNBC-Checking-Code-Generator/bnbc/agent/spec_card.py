"""Spec Card node: one strong-model call, disk wins, no human gate.

Disk layout: ``rules/<rule_id>/spec_card.yaml``. If the file exists it wins
entirely — the LLM is not called at all, so a human MAY correct a card
offline between runs. Otherwise one structured call to ``SPEC_MODEL_NAME``
produces the card, which is saved to disk.

No-HITL policy: the model must RESOLVE every interpretation ambiguity itself
and record the decision (with rationale and the rejected alternative) as a
convention. Unresolved items are logged into provenance, never block the run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from bnbc import config as cfg
from bnbc.contracts import DataPrerequisite, FixtureSketch, MeasurementConvention, SpecCard
from bnbc.llm import TokenMeter, call_llm, system, user

logger = logging.getLogger("bnbc.agent.spec_card")

SPEC_CARD_FILENAME = "spec_card.yaml"

SPEC_SYSTEM_PROMPT_TEMPLATE = """\
You are a building-code formalization expert. You turn one BNBC rule into a
Spec Card: the machine-readable interpretation contract used by code
generators, fixture planners, and reviewers.

Produce:
- conditions: every normative condition, RASE-style (id, requirement,
  applicability, exceptions, formula, thresholds with explicit units,
  required IFC data). Ids are short snake_case, e.g. "hook180_extension_min".
- conventions: every measurement/interpretation decision the rule text leaves
  ambiguous (e.g. where a hook extension is measured from). You MUST resolve
  each one yourself: pick the more conservative (safety-preserving) reading,
  record the decision, the rationale, AND the rejected alternative in the
  rationale text. Set status="proposed" (a human may later edit the card on
  disk; the pipeline does not wait for one).

  CONSISTENCY REQUIREMENT (the card is rejected otherwise): a convention must
  NEVER make your own fixture plan unsatisfiable. If a convention restricts
  which elements/models the rule applies to (an applicability predicate — set
  gates_applicability=true on it), then every fail/pass fixture sketch must
  satisfy that predicate, and you must state HOW in the convention's
  fixture_discharge field. NEVER adopt an "only if explicitly tagged X, else
  not_applicable" reading when neither the base models nor your sketches can
  establish that tag — choose the conservative reading that keeps the rule
  checkable on the fixture corpus and record the rejected alternative.
  Applicability must also be DEFAULT-INCLUSIVE on real models: production
  exports rarely carry designation tags or analysis data, so a predicate
  gated on their PRESENCE makes every real model not_applicable (observed
  live: a gate-accepted checker found "0 earthquake-resisting members" in
  every real file). Absence of a tag/datum means IN SCOPE; exclusion
  requires an explicit negative (tag=FALSE, load below threshold). Author
  the not_applicable fixture with that explicit negative.

  DATA-LOCATION consistency (same principle, for required data): every datum
  a condition needs must live where your own sketches can put it. The
  fixture engine attaches property sets to ELEMENTS (set_pset_property) —
  it can NOT create materials or attach material property sets. Conventions
  must therefore read required data (strengths, loads, tags) from the
  ELEMENT's own property sets; a material-level source may be named only as
  an additional fallback. Each pass/fail sketch must set_pset_property every
  required datum on the element it applies to.

  REAL-MODEL robustness: real exports frequently lack parametric profiles —
  a dimension convention that reads ONLY IfcRectangleProfileDef makes the
  checker blind on production models (observed: a gate-accepted checker
  skipped every real column as "non-rectangular profile"). Every
  dimension/thickness convention must declare a geometry fallback chain:
  parametric profile when present, else the body's bounding box.
- prerequisites: IFC data each condition needs (entity, requirement,
  on_missing verdict: "unknown" for missing data, "not_applicable" when the
  element class itself is absent).
- fixture_sketches: the MACHINE-EXECUTABLE fixture plan. Each sketch builds
  exactly ONE fixture file from `base_model`, and sketches are INDEPENDENT of
  each other — a sketch can never reference elements created by another
  sketch.
  * Simple perturbation: set `operator` to one of the operators below and
    `params` to its keyword arguments (lengths in mm, angles in degrees).
    `target` selects elements: {{"ifc_class": "...", "guid": "...",
    "name_contains": "...", "with_hook_angle_deg": N, "index": 0,
    "all": false}}.
  * Compound perturbation: set `steps` instead (never both) — a list of
    {{"operator": ..., "params": ..., "target": ...}} applied IN ORDER to the
    SAME model. Use this when one fixture needs several changes, e.g. insert
    two bars for a lap-splice condition, or insert a bar and then shorten its
    hook. A later step MAY target elements an earlier step inserted (match
    the bar_name you chose via name_contains).

  CRITICAL targeting rules: original elements in real base models have
  project-specific, non-standard names (e.g. stirrups are named "M_T1" or
  similar, not "stirrup").
  - To target original elements in real base models, use only `ifc_class` and
    `index` (or `all: true`). Never use `name_contains` (e.g. "stirrup") or
    `guid` on original base model elements.
  - Use `name_contains` (e.g. name_contains: "FIV") ONLY for elements
    inserted by an EARLIER STEP OF THE SAME SKETCH (insert_* operators).

  Set `base_model` to "__synthetic__" when the real corpus cannot exercise
  the condition — e.g. hook rules when no bar has a hook. On a synthetic
  base the FIRST step must be an insert_* operator; later steps may then
  modify what it inserted.

  The available real base models in the corpus are:
{base_models_list}

  When targeting a real model, set `base_model` to one of these exact filenames. When generating synthetic elements, set `base_model` to '__synthetic__'.

  REQUIRED coverage — the plan is rejected otherwise:
  * per condition: >=1 violating sketch (expected_verdict "fail"). For rules
    with <=3 conditions, one violating sketch per condition must sit at
    threshold-epsilon with boundary=true (larger rules: add boundary variants
    only where the budget allows);
  * per card: >=2 compliant sketches (expected_verdict "pass") — prefer ONE
    fully-compliant scene satisfying EVERY condition at once (a hand-built
    compliant micro-model) plus one compliant edge case near a threshold;
  * per card: >=1 data-degraded sketch (expected_verdict "unknown", e.g.
    strip_attribute/strip_pset) and >=1 out-of-scope sketch
    (expected_verdict "not_applicable", e.g. delete_elements_of_type).

  FIXTURE BUDGET — at most max(10, conditions+5) fixtures after
  deduplication. Over-budget plans are deterministically TRIMMED by the
  planner (coverage-preserving, drops logged), so author within budget to
  stay in control of what is kept:
  * identical perturbations are ONE fixture — NEVER author the same
    delete/strip/insert once per condition: a fixture's expected_verdict is
    MODEL-level, so a single not_applicable sketch (and a single unknown
    sketch) discharges the whole card;
  * prefer compound sketches whose one scene violates several conditions
    over near-duplicate single-condition sketches.

  VERDICT SOUNDNESS — expected_verdict asserts the verdict of the WHOLE
  perturbed model, not just the touched elements:
  * expected "pass" on a large real base model is almost always UNSOUND (you
    cannot know every other element complies) — build pass and fail sketches
    on "__synthetic__" bases where you control every element, and reserve
    real-model perturbations for not_applicable / unknown sketches;
  * synthetic scene recipe: step 1 insert_host_element (IfcBeam/IfcColumn/
    IfcWall/IfcSlab with exact dimensions), then insert bars/stirrups with
    host_name_contains referencing the host's name so they are related to it;
    size every dimension so the compliance arithmetic is exact and check it
    yourself (e.g. for a spacing limit of 300 mm, place bars 250 mm apart for
    pass, 350 mm for fail);
  * REPRESENTATION-IDIOM coverage: real exports are typically IFC2X3 with
    composite-curve bar geometry, while synthetic scaffolds default to IFC4
    indexed polycurves — a checker that only ever saw one idiom goes blind
    in production (observed: a gate-accepted hook checker flagged 100% of
    real hooks). When a rule MEASURES bar geometry (hooks, bends, radii),
    author at least one fail sketch variant with curve_style="composite" so
    the oracle spans both idioms;
  * RECOMPUTE derived thresholds PER SKETCH: when a threshold or an
    applicability trigger depends on scene quantities (e.g. 0.1*Ag*f'c
    scales with the cross-section), re-evaluate it for each sketch's actual
    dimensions and set the scene's data so every pass/fail sketch stays
    applicable (observed failure: constant FactoredAxialForce across
    growing sections silently pushed pass/fail scenes below an Ag-dependent
    trigger, making them not_applicable);
  * an "unknown" sketch must remove data that is NOT recoverable under your
    own conventions: if a convention declares a fallback (e.g. bbox-derived
    thickness when the attribute is missing), stripping only the primary
    source does NOT yield unknown — strip every declared source, or expect
    pass/fail instead;
  * applicability comes FIRST: a scene that fails your applicability
    conventions (e.g. an untagged element when your convention requires a
    tag) is not_applicable no matter what data is missing — an "unknown"
    sketch must SATISFY applicability (tag the synthetic scene) and then
    omit exactly the required datum;
  * a "pass" scene must CONTAIN every datum your conditions require (material
    strengths, dimensions, properties): compliant geometry with missing
    required data is unknown, not pass.

  Operator vocabulary:
{operator_vocabulary}

- ambiguities: leave EMPTY. Anything you would have listed here must instead
  be resolved as a convention (conservative reading, documented).

Be exhaustive but concise. Never invent thresholds not present in the rule.
"""


def build_spec_system_prompt(base_models: list[str] = None) -> str:
    """Render the system prompt with the live operator vocabulary and base models."""
    try:
        from bnbc.fixtures.planner import operator_vocabulary  # lazy: ifcopenshell

        vocab = operator_vocabulary()
    except Exception as exc:  # pragma: no cover — env without ifcopenshell
        logger.warning("operator vocabulary unavailable: %s", exc)
        vocab = "(operator vocabulary unavailable)"
    indented = "\n".join(f"  {line}" for line in vocab.splitlines())

    base_names = sorted(Path(b).name for b in (base_models or []))
    if base_names:
        base_list_str = "\n".join(f"  - {name}" for name in base_names)
    else:
        base_list_str = "  - (no base models found)"

    return SPEC_SYSTEM_PROMPT_TEMPLATE.format(
        operator_vocabulary=indented, base_models_list=base_list_str
    )


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------

def spec_card_path(rule_id: str, artifacts_dir: Path | str) -> Path:
    return Path(artifacts_dir) / rule_id / SPEC_CARD_FILENAME


def load_spec_card(rule_id: str, artifacts_dir: Path | str) -> SpecCard | None:
    """Load the (possibly human-edited) spec card from disk, or None."""
    path = spec_card_path(rule_id, artifacts_dir)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"spec card at {path} is not a mapping")
    return SpecCard.model_validate(data)


def save_spec_card(card: SpecCard, artifacts_dir: Path | str) -> Path:
    path = spec_card_path(card.rule_id, artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(card.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def open_items(card: SpecCard) -> list[str]:
    """Model-resolved interpretation decisions + leftovers (provenance record)."""
    items = [
        f"convention '{c.topic}' (status={c.status}): {c.decision}"
        for c in card.conventions
        if c.status != "adjudicated"
    ]
    items.extend(f"ambiguity: {a}" for a in card.ambiguities)
    return items


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

def rule_brief(state: dict) -> str:
    """The rule as the formaliser sees it: the curated statement, its scope,
    and the verbatim BNBC context ingest resolved from the rulebook.

    The statement says what must hold; the clauses, tables, equations and
    figures below it are the code's own words. When the two could be read
    differently, the clause wins — which is why both are here.
    """
    parts = [
        f"## Rule ID: {state['rule_id']}",
        f"## Rule Title: {state.get('rule_title', '')}",
        "",
        f"## Statement (curated)\n{state.get('rule_statement', '')}",
    ]
    if state.get("rule_scope_note"):
        parts.append(f"## Scope\n{state['rule_scope_note']}")
    if state.get("rule_context"):
        parts.append(f"## BNBC context\n{state['rule_context']}")
    return "\n".join(parts)


async def _generate_spec_card(state: dict, meter: TokenMeter) -> SpecCard:
    messages = [
        system(build_spec_system_prompt(state.get("base_models"))),
        user(rule_brief(state)),
    ]
    card: SpecCard = await call_llm(
        cfg.SPEC_MODEL_NAME, messages, meter, "spec_card", structured=SpecCard,
        reasoning_effort=cfg.SPEC_REASONING_EFFORT,
    )
    # Normalise fields the LLM must not control.
    card.rule_id = state["rule_id"]
    if not card.rule_title:
        card.rule_title = state.get("rule_title", "")
    if not card.source_text:
        card.source_text = state.get("rule_statement", "")
    for conv in card.conventions:
        if conv.status == "adjudicated":  # only humans adjudicate
            conv.status = "proposed"
    return card


def _card_yaml_without_sketches(card: SpecCard) -> str:
    """The spec-proper (conditions/conventions/prerequisites) as YAML.

    The fixture_sketches block is ~75% of a large card (observed: 1,065 of
    1,411 lines on 8.3.4.2) and re-dumping it verbatim into every regen/revise
    call pays that cost repeatedly — callers append a one-line-per-sketch
    summary instead, plus full YAML only for the sketches under repair.
    """
    data = card.model_dump(mode="json")
    data.pop("fixture_sketches", None)
    return yaml.safe_dump(data, sort_keys=False)


def _sketch_summary_lines(card: SpecCard) -> str:
    """One line per sketch: index, condition, verdict, base, operator chain."""
    lines = []
    for i, sk in enumerate(card.fixture_sketches):
        ops = "+".join(s.operator for s in sk.steps) if sk.steps else sk.operator
        lines.append(
            f"- [{i}] {sk.expected_verdict.value}"
            + (" boundary" if sk.boundary else "")
            + f" condition={sk.condition or '-'}"
            f" base={sk.base_model or '(default)'} ops={ops or '?'}"
            f" — {sk.perturbation[:90]}"
        )
    return "\n".join(lines) if lines else "(no sketches)"


def _sketches_yaml(card: SpecCard, indices: list[int]) -> str:
    """Full YAML for just the named sketch indices (the ones under repair)."""
    picked = {
        i: card.fixture_sketches[i].model_dump(mode="json")
        for i in sorted(set(indices))
        if 0 <= i < len(card.fixture_sketches)
    }
    if not picked:
        return "(none — all defects are card-level)"
    return yaml.safe_dump({f"sketch_{i}": sk for i, sk in picked.items()}, sort_keys=False)


class SketchRepair(BaseModel):
    index: Optional[int] = Field(
        None,
        description="Index into fixture_sketches to REPLACE or REMOVE; null to ADD a new sketch",
    )
    sketch: Optional[FixtureSketch] = Field(
        None,
        description="The replacement/new sketch; null together with an index REMOVES that sketch",
    )


class SketchRepairs(BaseModel):
    repairs: list[SketchRepair] = Field(default_factory=list)


def _sketch_repairable(defects: list[dict]) -> bool:
    """True when every defect is fixable by replacing/adding sketches.

    Sketch-indexed defects are; card-level coverage minimums are (additions);
    convention defects are NOT (they need a card-level regeneration).
    """
    if not defects:
        return False
    for d in defects:
        if d.get("sketch_index") is not None:
            continue
        msg = str(d.get("message", ""))
        if msg.startswith("condition ") or msg.startswith("card:"):
            continue
        return False
    return True


async def _repair_sketches(state: dict, meter: TokenMeter) -> SpecCard:
    """Targeted plan repair: replace ONLY the defective sketches.

    Whole-card regeneration resamples every sketch — on a 45-sketch card that
    fixes 5 defects and risks breaking 40 valid sketches (observed live:
    8.3.4.2 v3→v6 each regeneration traded one defect class for another).
    Here the valid sketches are kept verbatim and the model returns just the
    replacements/additions the defect list names.
    """
    previous = SpecCard.model_validate(state["spec_card"])
    defects = state.get("plan_defects") or []
    defect_lines = "\n".join(
        f"- [{'sketch ' + str(d['sketch_index']) if d.get('sketch_index') is not None else 'card-level'}] "
        f"{d.get('message', '')}"
        for d in defects
    )
    defect_indices = [d["sketch_index"] for d in defects if d.get("sketch_index") is not None]
    messages = [
        system(build_spec_system_prompt(state.get("base_models"))),
        user(
            content=(
                rule_brief(state)
                + "\n\n## Current Spec Card — spec proper (YAML, sketches summarised below)\n"
                + _card_yaml_without_sketches(previous)
                + "\n## Current fixture sketches (index summary — kept verbatim unless repaired)\n"
                + _sketch_summary_lines(previous)
                + "\n\n## Defective sketches (full YAML)\n"
                + _sketches_yaml(previous, defect_indices)
                + "\n## Planner defects to fix\n" + defect_lines + "\n\n"
                "Repair ONLY what the defects name. Return, for each "
                "sketch-indexed defect, a corrected replacement sketch with "
                "that exact index; for card-level coverage defects, return new "
                "sketches with index=null; to REMOVE a sketch (e.g. an "
                "over-budget duplicate), return its index with sketch=null. "
                "Every sketch you do NOT return is kept verbatim — do not "
                "restate valid sketches."
            )
        ),
    ]
    repairs: SketchRepairs = await call_llm(
        cfg.SPEC_MODEL_NAME, messages, meter, "spec_card", structured=SketchRepairs,
        reasoning_effort=cfg.SPEC_REASONING_EFFORT,
    )
    card = previous.model_copy(deep=True)
    replaced, added = 0, 0
    removals: list[int] = []
    for rep in repairs.repairs:
        if rep.sketch is None:
            if rep.index is not None and 0 <= rep.index < len(card.fixture_sketches):
                removals.append(rep.index)
            continue
        if rep.index is not None and 0 <= rep.index < len(card.fixture_sketches):
            card.fixture_sketches[rep.index] = rep.sketch
            replaced += 1
        else:
            card.fixture_sketches.append(rep.sketch)
            added += 1
    # Removals last, highest index first, so replacement indices stay valid.
    for idx in sorted(set(removals), reverse=True):
        del card.fixture_sketches[idx]
    card.rule_id = state["rule_id"]
    card.version = previous.version + 1
    logger.info(
        "Rule %s: sketch-level repair — %d replaced, %d added, %d removed (of %d defects)",
        state["rule_id"], replaced, added, len(set(removals)), len(defects),
    )
    return card


async def _regenerate_spec_card(state: dict, meter: TokenMeter) -> SpecCard:
    """One bounded retry: fix the fixture sketches the planner rejected."""
    previous = SpecCard.model_validate(state["spec_card"])
    messages = [
        system(build_spec_system_prompt(state.get("base_models"))),
        user(
            content=(
                rule_brief(state)
                + "\n\n## Your previous Spec Card — spec proper (YAML)\n"
                + _card_yaml_without_sketches(previous)
                + "\n## Your previous fixture sketches (summary)\n"
                + _sketch_summary_lines(previous)
                + "\n\n## The fixture planner REJECTED its sketches\n"
                + state.get("plan_errors", "")
                + "\n\nProduce the corrected Spec Card. Keep conditions, conventions "
                "and prerequisites unless they caused an error; author a fresh, "
                "budget-compliant fixture_sketches list so every requirement "
                "above is satisfied."
            )
        ),
    ]
    card: SpecCard = await call_llm(
        cfg.SPEC_MODEL_NAME, messages, meter, "spec_card", structured=SpecCard,
        reasoning_effort=cfg.SPEC_REASONING_EFFORT,
    )
    card.rule_id = state["rule_id"]
    card.version = previous.version + 1
    for conv in card.conventions:
        if conv.status == "adjudicated":  # only humans adjudicate
            conv.status = "proposed"
    return card


def summarize_gate_evidence(candidate: dict) -> str:
    """Distil a failed candidate's gate outcomes into (expected -> actual)
    counts with sample fixture ids — the evidence block for spec revision."""
    outcomes = (candidate.get("acceptance") or {}).get("outcomes") or []
    buckets: dict[tuple[str, str], list[str]] = {}
    for o in outcomes:
        if o.get("ok"):
            continue
        key = (str(o.get("expected_verdict")), str(o.get("actual_verdict") or "error"))
        buckets.setdefault(key, []).append(str(o.get("fixture_id", "?")))
    lines = [
        f"- expected {exp} -> got {act}: {len(fids)} fixture(s) "
        f"(e.g. {', '.join(fids[:6])})"
        for (exp, act), fids in sorted(buckets.items())
    ]
    return "\n".join(lines) if lines else "(no failed fixture outcomes)"


class SpecRevision(BaseModel):
    """Targeted spec revision: replace only the implicated blocks.

    A full-card rewrite made the model re-emit every sketch verbatim (16K
    output tokens on 8.3.4.2's 48-sketch card) and risked silently degrading
    untouched blocks; a revision patches at the level of the fault instead.
    """

    conventions: Optional[list[MeasurementConvention]] = Field(
        None,
        description="Full replacement conventions list, or null to keep the current one",
    )
    prerequisites: Optional[list[DataPrerequisite]] = Field(
        None,
        description="Full replacement prerequisites list, or null to keep the current one",
    )
    sketch_repairs: list[SketchRepair] = Field(
        default_factory=list,
        description=(
            "Sketch changes: index+sketch replaces, index+null sketch removes, "
            "null index adds. Unmentioned sketches are kept verbatim."
        ),
    )
    revision_note: str = Field(
        "", description="One-paragraph record of what was inconsistent and how this resolves it"
    )


async def _revise_spec_card(state: dict, meter: TokenMeter) -> SpecCard:
    """One bounded outer-loop revision, fed the gate's oracle-fault evidence.

    Triggered when the gate shows a SYSTEMATIC verdict-class mismatch (e.g.
    every fail/pass fixture returned not_applicable): the checker implements
    the card, so the card — its applicability conventions, prerequisites, or
    sketches — is what must change, not the code.
    """
    previous = SpecCard.model_validate(state["spec_card"])
    evidence = summarize_gate_evidence(state.get("candidate") or {})
    messages = [
        system(build_spec_system_prompt(state.get("base_models"))),
        user(
            content=(
                rule_brief(state)
                + "\n\n## Your previous Spec Card — spec proper (YAML)\n"
                + _card_yaml_without_sketches(previous)
                + "\n## Your previous fixture sketches (index summary)\n"
                + _sketch_summary_lines(previous)
                + "\n\n## Gate evidence: the card is internally inconsistent\n"
                "A checker implementing this card was executed against the "
                "card's own fixtures. Nearly every fail/pass fixture returned "
                "one uniform wrong verdict class:\n" + evidence + "\n\n"
                "This is an ORACLE fault, not a code fault: the card's "
                "applicability conventions/prerequisites put its own fixtures "
                "out of scope (or starve them of required data). Return a "
                "TARGETED revision so every fail/pass sketch is applicable and "
                "evaluable under the card's own conventions — either (a) a "
                "replacement conventions/prerequisites list that weakens or "
                "re-scopes what causes the mismatch (prefer the conservative "
                "reading that keeps the rule checkable on the fixture corpus; "
                "record the rejected alternative), or (b) sketch_repairs that "
                "make the fixtures satisfy it. Change ONLY what the evidence "
                "implicates; every block you return as null and every sketch "
                "you do not mention is kept verbatim. Conventions must stay "
                "implementable from IFC data actually present in the fixtures "
                "(never require an attribute the IFC schema does not define "
                "for that class)."
            )
        ),
    ]
    revision: SpecRevision = await call_llm(
        cfg.SPEC_MODEL_NAME, messages, meter, "spec_revise", structured=SpecRevision,
        reasoning_effort=cfg.SPEC_REASONING_EFFORT,
    )
    card = previous.model_copy(deep=True)
    if revision.conventions is not None:
        card.conventions = list(revision.conventions)
    if revision.prerequisites is not None:
        card.prerequisites = list(revision.prerequisites)
    removals: list[int] = []
    for rep in revision.sketch_repairs:
        if rep.sketch is None:
            if rep.index is not None and 0 <= rep.index < len(card.fixture_sketches):
                removals.append(rep.index)
            continue
        if rep.index is not None and 0 <= rep.index < len(card.fixture_sketches):
            card.fixture_sketches[rep.index] = rep.sketch
        else:
            card.fixture_sketches.append(rep.sketch)
    for idx in sorted(set(removals), reverse=True):
        del card.fixture_sketches[idx]
    card.rule_id = state["rule_id"]
    card.version = previous.version + 1
    for conv in card.conventions:
        if conv.status == "adjudicated":  # only humans adjudicate
            conv.status = "proposed"
    if revision.revision_note:
        logger.info("Rule %s: spec revision note: %s", state["rule_id"], revision.revision_note)
    return card


async def spec_revise_node(state: dict) -> dict:
    """Bounded outer loop (max cfg.MAX_SPEC_REVISIONS): revise the spec card
    from gate evidence, then rebuild fixtures and re-draft from scratch."""
    rule_id = state["rule_id"]
    artifacts_dir = Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR)
    meter = TokenMeter.from_state(state.get("token_usage"))

    if state.get("spec_card_source") == "disk":
        logger.warning(
            "Rule %s: revising a disk-sourced (possibly human-edited) spec card "
            "from gate evidence — previous version remains in git history", rule_id,
        )
    card = await _revise_spec_card(state, meter)
    path = save_spec_card(card, artifacts_dir)
    logger.info("Rule %s: spec card revised from gate evidence (v%d), saved to %s",
                rule_id, card.version, path)

    items = open_items(card)
    return {
        "spec_card": card.model_dump(mode="json"),
        "spec_card_source": "revised",
        "spec_revisions": int(state.get("spec_revisions") or 0) + 1,
        "adjudication_status": "adjudicated" if card.is_adjudicated else "model_resolved",
        "adjudication_open_items": items,
        # Fresh start for the drafter: the old candidate implemented the old
        # card, so its evidence and best-of-run snapshot do not carry over
        # (scores against the old fixtures are stale).
        "candidate": {},
        "best_candidate": {},
        # The revised card gets a fresh (bounded) plan-repair allowance:
        # observed live on 8.3.5.1, a revision arrived at the planner with
        # the regen budget already spent by pre-draft repairs and died
        # terminally on a repairable defect. Total is still bounded by
        # (1 + MAX_SPEC_REVISIONS) x MAX_PLAN_REGENS.
        "spec_plan_regens": 0,
        "token_usage": meter.to_state(),
        "status": "spec_revised",
    }


async def spec_card_node(state: dict) -> dict:
    """Spec-card node: disk wins entirely; else generate once and save.

    When routed back with ``plan_invalid`` the card is regenerated ONCE with
    the planner's defect list (disk-wins is bypassed — the disk card is the
    invalid one just written).
    """
    rule_id = state["rule_id"]
    artifacts_dir = Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR)
    meter = TokenMeter.from_state(state.get("token_usage"))
    regenerating = state.get("status") == "plan_invalid" and bool(state.get("plan_errors"))

    if regenerating:
        if _sketch_repairable(state.get("plan_defects") or []):
            source = "sketch_repaired"
            try:
                card = await _repair_sketches(state, meter)
            except Exception as exc:
                # A too-big repair (structured length limit, unparseable
                # output) must degrade to whole-card regeneration — writing a
                # fresh budget-sized sketch list is a SMALLER output than
                # reasoning about removals (observed live 2026-07-17: a bulk
                # repair burned 22K reasoning tokens into the completion cap
                # and the rule died terminally with retry budget unspent).
                from bnbc.llm import BudgetExceeded, is_llm_infra_error

                if isinstance(exc, BudgetExceeded) or not is_llm_infra_error(exc):
                    raise
                logger.warning(
                    "Rule %s: sketch repair failed (%s) — degrading to "
                    "whole-card regeneration", rule_id, exc,
                )
                source = "regenerated"
                card = await _regenerate_spec_card(state, meter)
        else:
            source = "regenerated"
            card = await _regenerate_spec_card(state, meter)
        path = save_spec_card(card, artifacts_dir)
        logger.info("Rule %s: spec card %s (v%d) after plan errors, saved to %s",
                    rule_id, source, card.version, path)
    else:
        card = load_spec_card(rule_id, artifacts_dir)
        if card is not None:
            source = "disk"
            logger.info("Rule %s: spec card loaded from disk (human-adjudicated wins)", rule_id)
        else:
            source = "generated"
            card = await _generate_spec_card(state, meter)
            path = save_spec_card(card, artifacts_dir)
            logger.info("Rule %s: spec card generated and saved to %s", rule_id, path)

    # No-HITL (D6'): interpretation decisions are recorded for provenance and
    # the card stays editable on disk, but nothing ever waits on a human.
    items = open_items(card)
    if items:
        logger.info(
            "Rule %s: %d model-resolved interpretation decision(s) recorded "
            "(editable in %s):\n%s",
            rule_id, len(items), spec_card_path(rule_id, artifacts_dir),
            "\n".join(f"  - {i}" for i in items),
        )
    return {
        "spec_card": card.model_dump(mode="json"),
        "spec_card_source": source,
        "spec_plan_regens": (
            int(state.get("spec_plan_regens") or 0) + (1 if regenerating else 0)
        ),
        "adjudication_status": "adjudicated" if card.is_adjudicated else "model_resolved",
        "adjudication_open_items": items,
        "token_usage": meter.to_state(),
        "status": "spec_ready",
    }
