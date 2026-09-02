"""Fixture planner: spec-card sketches -> validated, executable FixtureSpecs.

The spec card's ``fixture_sketches`` are authored (by the spec-card model or
a human) against the *operator vocabulary* — this module never interprets
prose. :func:`plan_fixtures` validates every sketch against the registered
operators, the base-model census (do the targeted classes/names exist at
all?), the plan minimums (per condition: >=1 violating sketch, boundary
required only on small rules; per card: >=2 compliant, >=1 unknown, >=1
not_applicable) and the fixture budget, then DEDUPLICATES identical
perturbations across conditions (a fixture's verdict is model-level, so one
"delete all beams" file discharges not_applicable for every condition at
once — observed live: 8.3.4.2 authored 9 identical NA sketches). It raises
:class:`~bnbc.fixtures.errors.PlanningError` carrying a structured defect
list otherwise — the pipeline repairs the defective sketches (or rejects
the rule); nothing is silently skipped.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

import logging

from bnbc.contracts import FixtureSketch, FixtureSpec, FixtureStep, SpecCard, Verdict
from bnbc.fixtures import measure
from bnbc.fixtures.errors import PlanningError, TargetNotFoundError
from bnbc.fixtures.operators import OPERATORS

logger = logging.getLogger("bnbc.fixtures.planner")

#: Base model name meaning "build a minimal scaffold instead of opening a file".
SYNTHETIC_BASE = "__synthetic__"

#: Fixture budget floor: the per-rule cap is max(this, n_conditions + 5),
#: counted AFTER deduplication. Every fixture is a subprocess run per gate
#: execution per evaluate call — 48 fixtures on a 4,300-element base model
#: made the 8.3.4.2 gate the wall-clock bottleneck.
FIXTURE_BUDGET_FLOOR = int(os.environ.get("MAX_FIXTURES_PER_RULE", "10"))

#: Boundary (threshold±epsilon) fail sketches are only REQUIRED on rules with
#: at most this many conditions; larger rules stay within budget by covering
#: each condition once and adding boundary variants only where they fit.
BOUNDARY_REQUIRED_MAX_CONDITIONS = 3


def fixture_budget(n_conditions: int) -> int:
    """Per-rule fixture cap: enough for 1 kill fixture per condition + shared
    pass/unknown/not_applicable coverage, never below the configured floor."""
    return max(FIXTURE_BUDGET_FLOOR, n_conditions + 5)


# ---------------------------------------------------------------------------
# Operator vocabulary (rendered into the spec-card prompt)
# ---------------------------------------------------------------------------

def operator_vocabulary() -> str:
    """One entry per registered operator: name(params): first docstring
    PARAGRAPH (collapsed to one line).

    The full first paragraph, not the first line: single-line truncation cut
    the orientation semantics off insert_host_element and the spec model
    authored every column sideways (observed live on 8.3.5.1) — an operator's
    parameter semantics must reach the model that authors its parameters.
    """
    lines = []
    for name in sorted(OPERATORS):
        cls = OPERATORS[name]
        try:
            sig = inspect.signature(cls._apply)
            params = [
                p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
                for p in sig.parameters.values()
                if p.kind == inspect.Parameter.KEYWORD_ONLY and p.name != "_ignored"
            ]
        except (TypeError, ValueError):
            params = ["..."]
        doc = (inspect.getdoc(cls) or "").split("\n\n", 1)[0]
        doc = " ".join(part.strip() for part in doc.splitlines()).strip()
        lines.append(f"- {name}({', '.join(params)}): {doc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Target selector DSL
# ---------------------------------------------------------------------------

def build_selector(target: dict[str, Any]) -> Callable:
    """Deterministic element selector from a target spec dict.

    Keys: ``ifc_class`` (required), ``guid`` or ``global_id`` (exact lookup),
    ``name_contains``, ``with_hook_angle_deg`` (terminal hook within 15 deg
    at either end), ``all`` (default False), ``index`` (default 0, applied
    when ``all`` is false). Selection order is file order, so the same file
    always yields the same targets.
    """
    ifc_class = target.get("ifc_class")
    if not ifc_class:
        raise PlanningError(f"target spec needs 'ifc_class': {target!r}")
    guid = target.get("guid") or target.get("global_id")
    name_contains = target.get("name_contains")
    hook_angle = target.get("with_hook_angle_deg")
    take_all = bool(target.get("all", False))
    index = int(target.get("index", 0))

    def _selector(model) -> list:
        if guid:
            try:
                el = model.by_guid(str(guid))
            except (RuntimeError, KeyError, Exception):
                el = None
            if el is None:
                raise TargetNotFoundError(f"target spec guid {guid!r} not found in model")
            if not el.is_a(ifc_class):
                raise TargetNotFoundError(
                    f"target spec guid {guid!r} found but is a {el.is_a()}, not {ifc_class}"
                )
            return [el]

        elements = list(model.by_type(ifc_class))
        if name_contains:
            needle = str(name_contains).lower()
            elements = [e for e in elements if needle in str(getattr(e, "Name", "") or "").lower()]
        if hook_angle is not None:
            matched = []
            for el in elements:
                for end in ("end", "start"):
                    hook = measure.terminal_hook(el, end=end)
                    if hook is not None and abs(hook.angle_deg - float(hook_angle)) <= 15.0:
                        matched.append(el)
                        break
            elements = matched
        if take_all:
            return elements
        if index >= len(elements):
            raise TargetNotFoundError(
                f"target {target!r}: index {index} out of range ({len(elements)} matched)"
            )
        return [elements[index]]

    return _selector


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def sketch_steps(sk: FixtureSketch) -> list[FixtureStep]:
    """Normalise a sketch to its operator chain (single-op is a 1-step chain)."""
    if sk.steps:
        return list(sk.steps)
    return [FixtureStep(operator=sk.operator, params=dict(sk.params), target=dict(sk.target))]


def _content_hash(base_model: str, steps: list[FixtureStep]) -> str:
    """Content address of a fixture: base model + full operator chain."""
    payload = {
        "base": base_model,
        "steps": [
            {"operator": s.operator, "params": s.params, "target": s.target}
            for s in steps
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]


def _inserted_names(steps: list[FixtureStep], upto: int) -> list[str]:
    """Names given to elements inserted by steps before index ``upto``."""
    names = []
    for step in steps[:upto]:
        for key in ("bar_name", "new_name", "name"):
            val = step.params.get(key)
            if val:
                names.append(str(val))
    return names


def _infer_target_class(steps: list[FixtureStep], upto: int, target: dict) -> Optional[str]:
    """Infer a missing target ``ifc_class`` from earlier steps of the sketch.

    Spec models habitually omit ``ifc_class`` in nested step targets that
    reference an element the SAME sketch inserted (observed live on 8.3.5.1:
    the omission recurred through two repair calls and a revision, burning
    the whole plan budget on a mechanical defect). When the ``name_contains``
    needle matches exactly one inserted element name, the class is
    unambiguous — infer it instead of rejecting the plan (Postel's law at
    the LLM boundary, ledger I9).
    """
    needle = str(target.get("name_contains") or "").lower()
    if not needle:
        return None
    classes: set[str] = set()
    for step in steps[:upto]:
        params = step.params or {}
        if step.operator == "insert_host_element":
            name = str(params.get("name") or "")
            if name and needle in name.lower():
                classes.add(str(params.get("ifc_class") or "IfcBeam"))
        elif step.operator and step.operator.startswith("insert_"):
            name = str(params.get("bar_name") or "")
            if name and needle in name.lower():
                classes.add("IfcReinforcingBar")
    return next(iter(classes)) if len(classes) == 1 else None


def _schema_attribute_exists(ifc_class: str, attribute: str) -> Optional[bool]:
    """Whether ``ifc_class`` declares direct attribute ``attribute`` in ANY
    supported IFC schema (IFC4, IFC2X3). None = undeterminable, skip check.

    Conservative on purpose: True if any schema has it, False only when the
    class is known to at least one schema and none declares the attribute
    (observed live: a spec convention stripped 'Thickness' off IfcWall — no
    such attribute exists — and the build crashed terminally).
    """
    try:
        import ifcopenshell  # lazy

        wrapper = ifcopenshell.ifcopenshell_wrapper
    except Exception:
        return None
    found_class = False
    for schema_name in ("IFC4", "IFC2X3"):
        try:
            decl = wrapper.schema_by_name(schema_name).declaration_by_name(ifc_class)
            entity = decl.as_entity()
            if entity is None:
                continue
            found_class = True
            if any(a.name() == attribute for a in entity.all_attributes()):
                return True
        except Exception:
            continue
    return False if found_class else None


def _validate_target_against_census(
    census: dict[str, Any], step: FixtureStep, prior_names: list[str]
) -> Optional[str]:
    """Defect message when a real-base target cannot possibly resolve, else None.

    Best-effort static check: class existence, index range, and name_contains
    reachability (either a censused name or a name inserted by an earlier
    step of the same sketch). Geometry predicates (with_hook_angle_deg) and
    GUIDs are left to build time.
    """
    target = step.target or {}
    ifc_class = target.get("ifc_class")
    if not ifc_class:
        return None
    bucket = census.get(ifc_class)
    needle = str(target.get("name_contains") or "").lower()
    if bucket is None:
        if needle and any(needle in n.lower() for n in prior_names):
            return None  # targets an element this sketch inserts first
        return f"base model has no {ifc_class} elements"
    if needle:
        if any(needle in n.lower() for n in prior_names):
            return None
        if not any(needle in n.lower() for n in bucket.get("names", [])):
            return (
                f"no {ifc_class} name contains {needle!r} in the base model "
                "(and no earlier step of this sketch inserts one — "
                "name_contains may only reference censused names or names "
                "your own earlier insert_* step assigned)"
            )
        return None
    if target.get("guid") or target.get("global_id") or \
            target.get("with_hook_angle_deg") is not None:
        return None  # left to build time
    if not target.get("all", False):
        index = int(target.get("index", 0))
        if index >= int(bucket.get("count", 0)):
            return (
                f"target index {index} out of range — base model has only "
                f"{bucket.get('count', 0)} {ifc_class} element(s)"
            )
    return None


def _trim_to_budget(
    card: SpecCard,
    groups: dict[tuple[str, str], list[int]],
    group_order: list[tuple[str, str]],
    budget: int,
) -> list[tuple[str, str]]:
    """Coverage-preserving deterministic trim of an over-budget fixture plan.

    Keep priority: (1) one kill fixture per condition — boundary variants
    first, then synthetic bases, then authoring order; (2) two compliant
    fixtures — synthetic first; (3) one unknown and one not_applicable;
    (4) remaining slots refill in authoring order. Every dropped fixture is
    logged with its perturbation ("no silent caps").
    """
    def _info(key: tuple[str, str]) -> dict:
        indices = groups[key]
        sketches = [card.fixture_sketches[i] for i in indices]
        return {
            "verdict": sketches[0].expected_verdict,
            "boundary": any(sk.boundary for sk in sketches),
            "synthetic": key[0] == SYNTHETIC_BASE,
            "conditions": {
                sk.condition for sk in sketches
                if sk.expected_verdict == Verdict.FAIL and sk.condition
            },
            "perturbation": sketches[0].perturbation,
        }

    infos = {key: _info(key) for key in group_order}

    def _rank(key: tuple[str, str]) -> tuple:
        info = infos[key]
        return (not info["boundary"], not info["synthetic"], group_order.index(key))

    keep: set[tuple[str, str]] = set()

    # (1) kill coverage: greedy per condition in card order.
    covered: set[str] = set()
    for cond in card.conditions:
        if cond.id in covered:
            continue
        candidates = [
            key for key in group_order
            if infos[key]["verdict"] == Verdict.FAIL and cond.id in infos[key]["conditions"]
        ]
        if not candidates:
            continue
        best = min(candidates, key=_rank)
        keep.add(best)
        covered |= infos[best]["conditions"]

    # (2) two compliant fixtures, synthetic first.
    passes = sorted(
        (k for k in group_order if infos[k]["verdict"] == Verdict.PASS), key=_rank
    )
    keep.update(passes[:2])

    # (3) one unknown, one not_applicable.
    for verdict in (Verdict.UNKNOWN, Verdict.NOT_APPLICABLE):
        firsts = [k for k in group_order if infos[k]["verdict"] == verdict]
        if firsts:
            keep.add(min(firsts, key=_rank))

    # (4) refill any remaining budget in authoring order.
    for key in group_order:
        if len(keep) >= budget:
            break
        keep.add(key)

    kept_order = [k for k in group_order if k in keep]
    dropped = [k for k in group_order if k not in keep]
    for key in dropped:
        info = infos[key]
        logger.info(
            "Plan budget trim: dropped %s fixture (%s) — %s",
            info["verdict"].value,
            "boundary" if info["boundary"] else "non-boundary",
            str(info["perturbation"])[:100],
        )
    logger.info(
        "Plan budget trim: kept %d/%d fixtures (budget %d); condition kill "
        "coverage, 2 pass, unknown and not_applicable preserved",
        len(kept_order), len(group_order), budget,
    )
    return kept_order


def plan_fixtures(spec_card: SpecCard | dict, base_models: list[str]) -> list[FixtureSpec]:
    """Validate the card's fixture sketches and materialise them as FixtureSpecs.

    Raises :class:`PlanningError` whose ``defects`` list is structured
    (``sketch_index`` per record, ``None`` for card-level defects) so the
    pipeline can repair individual sketches instead of regenerating the card.
    """
    card = spec_card if isinstance(spec_card, SpecCard) else SpecCard.model_validate(spec_card)
    base_names = [Path(b).name for b in base_models]
    base_paths = {Path(b).name: Path(b) for b in base_models}
    default_base = base_names[0] if base_names else SYNTHETIC_BASE
    condition_ids = {c.id for c in card.conditions}

    defects: list[dict] = []

    def _defect(index: Optional[int], message: str) -> None:
        defects.append({"sketch_index": index, "message": message})

    if not card.fixture_sketches:
        _defect(None, "spec card has no fixture_sketches")

    censuses: dict[str, Optional[dict]] = {}

    def _census_for(base: str) -> Optional[dict]:
        if base not in censuses:
            from bnbc.fixtures.census import load_census  # lazy: ifcopenshell

            path = base_paths.get(base)
            censuses[base] = load_census(path) if path else None
        return censuses[base]

    for i, sk in enumerate(card.fixture_sketches):
        label = f"sketch #{i} ({sk.condition or 'no condition'})"
        # When `steps` exist they fully determine materialisation; LLMs still
        # fill the sketch-level `operator` as a salient-step label (observed
        # live echoing step 0 AND naming mid-chain operators). It is inert —
        # ignore it rather than reject the plan over a label.
        if not sk.steps and not sk.operator:
            _defect(i, f"{label}: missing operator (vocabulary below)")
        steps = sketch_steps(sk) if (sk.steps or sk.operator) else []
        base = sk.base_model or default_base
        census = _census_for(base) if base != SYNTHETIC_BASE else None
        for j, step in enumerate(steps):
            slabel = f"{label} step {j}" if sk.steps else label
            if not step.operator:
                _defect(i, f"{slabel}: missing operator (vocabulary below)")
                continue
            if step.operator not in OPERATORS:
                _defect(i, f"{slabel}: unknown operator {step.operator!r}")
                continue
            # Declarative parameter domains: degenerate params are plan-time
            # defects (with sketch index), never build-time crashes.
            cls = OPERATORS[step.operator]
            for pname, (lo, hi) in (getattr(cls, "PARAM_DOMAINS", None) or {}).items():
                val = step.params.get(pname)
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    continue
                if (lo is not None and val < lo) or (hi is not None and val > hi):
                    note = getattr(cls, "DOMAIN_NOTE", "")
                    _defect(
                        i,
                        f"{slabel}: {pname}={val} outside {step.operator} domain "
                        f"[{lo if lo is not None else '-inf'}, "
                        f"{hi if hi is not None else 'inf'}]"
                        + (f" — {note}" if note else ""),
                    )
            # strip_attribute may only null DIRECT schema attributes — a
            # convention-invented attribute must fail at plan time with
            # guidance, not crash the build.
            if step.operator == "strip_attribute" and step.target.get("ifc_class"):
                attr = str(step.params.get("attribute") or "")
                if attr and _schema_attribute_exists(
                    str(step.target["ifc_class"]), attr
                ) is False:
                    _defect(
                        i,
                        f"{slabel}: {step.target['ifc_class']} declares no direct "
                        f"attribute {attr!r} in the IFC schema — strip_attribute "
                        "only nulls direct attributes; use strip_pset for "
                        "property-set values, or target a class that has the "
                        "attribute",
                    )
            if step.target and "ifc_class" not in step.target:
                inferred = _infer_target_class(steps, j, step.target)
                if inferred:
                    step.target["ifc_class"] = inferred
                    logger.info(
                        "%s: inferred target ifc_class=%s from the sketch's "
                        "own inserted element", slabel, inferred,
                    )
            if step.target and "ifc_class" not in step.target:
                _defect(
                    i,
                    f"{slabel}: target spec needs 'ifc_class' (not inferable — "
                    "no earlier insert step of this sketch names a matching "
                    "element)",
                )
            elif census is not None and step.target:
                # Static target reachability against the base-model census —
                # fail at plan time (seconds) instead of build time (minutes).
                problem = _validate_target_against_census(
                    census, step, _inserted_names(steps, j)
                )
                if problem:
                    _defect(i, f"{slabel}: {problem}")
        if base != SYNTHETIC_BASE and base not in base_names:
            _defect(i, f"{label}: base_model {base!r} not among {base_names}")
        if base == SYNTHETIC_BASE and steps and steps[0].operator and \
                not steps[0].operator.startswith("insert_"):
            _defect(
                i,
                f"{label}: on a synthetic base the FIRST step must be an "
                f"insert_* operator (got {steps[0].operator!r}); later steps "
                "may then target the inserted elements",
            )
        if sk.expected_verdict == Verdict.FAIL and sk.condition not in condition_ids:
            _defect(
                i,
                f"{label}: fail sketch must name a spec condition id "
                f"(known: {sorted(condition_ids)})",
            )

    # Convention-oracle consistency: an applicability-gating convention must
    # say how the card's own fail/pass fixtures satisfy it — otherwise the
    # convention can silently force every fixture to not_applicable and the
    # gate can never accept (observed live on 8.3.4.2).
    for conv in card.conventions:
        if conv.gates_applicability and not conv.fixture_discharge.strip():
            _defect(
                None,
                f"convention '{conv.topic}' gates applicability but has no "
                "fixture_discharge — state how the fail/pass sketches satisfy "
                "the applicability predicate, or weaken the convention so the "
                "rule stays checkable on the fixture corpus",
            )

    # ---- Dedup groups (computed pre-defect-check so the budget counts real
    # fixtures, and contradictions surface as plan defects, not gate noise).
    # Key = (base, content hash): the exact perturbation. Sketches with the
    # same key produce byte-identical files — they are ONE fixture, and for
    # fail sketches the merged fixture asserts the UNION of their conditions.
    groups: dict[tuple[str, str], list[int]] = {}
    group_order: list[tuple[str, str]] = []
    for i, sk in enumerate(card.fixture_sketches):
        if not (sk.steps or sk.operator):
            continue  # already a defect above
        base = sk.base_model or default_base
        key = (base, _content_hash(base, sketch_steps(sk)))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(i)
    for key, indices in groups.items():
        by_verdict: dict[str, list[int]] = {}
        for i in indices:
            by_verdict.setdefault(card.fixture_sketches[i].expected_verdict.value, []).append(i)
        if len(by_verdict) > 1:
            listing = "; ".join(
                f"{v}: #{', #'.join(str(i) for i in idxs)}"
                for v, idxs in sorted(by_verdict.items())
            )
            _defect(
                indices[-1],
                f"sketches {', '.join('#' + str(i) for i in indices)} apply the "
                f"IDENTICAL perturbation to the same base model but expect a "
                f"different verdict ({listing}) — an identical perturbation has "
                "exactly one model-level verdict; reconcile ALL of them: keep "
                "one with the correct whole-model verdict and remove or replace "
                "the rest",
            )

    # Plan minimums — the fixture-budget coverage policy:
    #   per condition: >=1 kill (fail) sketch; boundary variants are required
    #   only on small rules (<= BOUNDARY_REQUIRED_MAX_CONDITIONS conditions);
    #   per card: >=2 compliant (pass), >=1 unknown, >=1 not_applicable.
    n_conditions = len(condition_ids)
    boundary_required = n_conditions <= BOUNDARY_REQUIRED_MAX_CONDITIONS
    for cid in sorted(condition_ids):
        fails = [sk for sk in card.fixture_sketches
                 if sk.condition == cid and sk.expected_verdict == Verdict.FAIL]
        if not fails:
            _defect(None, f"condition {cid}: needs >=1 violating sketch (expected_verdict=fail)")
        elif boundary_required and not any(sk.boundary for sk in fails):
            _defect(None, f"condition {cid}: needs a boundary=true violating sketch")
    n_pass = sum(1 for sk in card.fixture_sketches if sk.expected_verdict == Verdict.PASS)
    if n_pass < 2:
        _defect(
            None,
            f"card: needs >=2 compliant sketches (expected_verdict=pass, has {n_pass}) — "
            "prefer one fully-compliant scene satisfying EVERY condition plus one "
            "compliant edge case",
        )
    # An unknown fixture is required only when the card's own semantics can
    # PRODUCE unknown (some prerequisite declares on_missing=unknown).
    # Demanding one otherwise forces an inconsistent oracle: with total
    # fallback conventions (bbox dims, default-in-scope triggers) no
    # manufacturable scene is legitimately unknown (observed live: the
    # forced unknown sketch was 8.3.5.1's first oracle bug).
    card_can_be_unknown = any(
        p.on_missing == Verdict.UNKNOWN for p in card.prerequisites
    )
    if card_can_be_unknown and not any(
        sk.expected_verdict == Verdict.UNKNOWN for sk in card.fixture_sketches
    ):
        _defect(
            None,
            "card: needs >=1 data-degraded sketch (expected_verdict=unknown) — "
            "a prerequisite declares on_missing=unknown, so the unknown path "
            "must be exercised",
        )
    if not any(sk.expected_verdict == Verdict.NOT_APPLICABLE for sk in card.fixture_sketches):
        _defect(None, "card: needs >=1 out-of-scope sketch (expected_verdict=not_applicable)")

    if defects:
        raise PlanningError(
            "fixture plan invalid:\n"
            + "\n".join(f"  - {d['message']}" for d in defects)
            + "\n\nOperator vocabulary:\n" + operator_vocabulary(),
            defects=defects,
        )

    # Fixture budget, enforced DETERMINISTICALLY on the deduplicated set.
    # Choosing which redundant fixtures to drop is not an LLM task: a bulk
    # "remove 15 sketches" repair burned 22K reasoning tokens into the
    # completion cap and killed the run (observed live 2026-07-17). The
    # planner keeps a coverage-preserving core and logs every drop.
    budget = fixture_budget(n_conditions)
    if len(group_order) > budget:
        group_order = _trim_to_budget(card, groups, group_order, budget)

    specs: list[FixtureSpec] = []
    merged_away = 0
    for out_idx, key in enumerate(group_order):
        indices = groups[key]
        sk = card.fixture_sketches[indices[0]]
        merged_away += len(indices) - 1
        steps = sketch_steps(sk)
        first = steps[0]
        base, content_hash = key
        params = dict(first.params)
        if first.target:
            params["target"] = dict(first.target)
        # A merged fail fixture asserts every condition its duplicates named.
        conditions: list[str] = []
        for i in indices:
            dup = card.fixture_sketches[i]
            if dup.expected_verdict == Verdict.FAIL and dup.condition \
                    and dup.condition not in conditions:
                conditions.append(dup.condition)
        specs.append(FixtureSpec(
            fixture_id=f"{card.rule_id}::F{out_idx:02d}",
            rule_id=card.rule_id,
            base_model=base,
            operator=first.operator,
            params=params,
            steps=steps,
            content_hash=content_hash,
            source_sketch_indices=list(indices),
            # Content-addressed name: unchanged sketches keep a findable file
            # across card versions, so the manifest builder reuses instead of
            # rebuilding (an 8-condition card rebuilds ~45 fixtures otherwise).
            file_name=f"F{out_idx:02d}_{first.operator}_{content_hash}.ifc",
            expected_verdict=sk.expected_verdict,
            expected_conditions=conditions,
        ))
    if merged_away:
        logger.info(
            "Plan dedup: %d duplicate sketch(es) merged into %d fixture(s)",
            merged_away, len(specs),
        )
    return specs
