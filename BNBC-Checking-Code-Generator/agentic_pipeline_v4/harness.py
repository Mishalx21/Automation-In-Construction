"""
Version 4 -- adds three GENERALIZED failure-class classifiers on top of the
v3 judge (v3's accept/reject decision is unchanged; v4 only sharpens *why*
a case failed, so a human/future-drafter gets an actionable, class-specific
diagnosis instead of a generic "wrong verdict"/"false confirmation").

Each classifier triggers on a STRUCTURAL property of the fixture/checker,
never on a rule id, so it applies to any current or future rule that shares
the same shape of problem:

  Stage B -- DEGENERATE_POPULATION
    Triggers whenever a Positive fixture returns not_applicable/pass AND
    the paired Negative baseline had a non-zero population of the fixture's
    declared element_type that the fixture reduced to zero. Generalizes the
    S5 finding (checker bails out when an entire population vanishes,
    instead of treating total loss as a violation in its own right).

  Stage C -- NULL_AMBIGUITY
    Triggers whenever a fixture's own meta shows a real "before" value and
    a blank/removed "after" value for a mutable attribute. Generalizes the
    A3 finding (checker conflates "never designated" with "was designated,
    then removed" -- the latter is usually itself the violation).

  Stage A -- GEOMETRY_OUTLIER (schema-native, revised)
    Triggers when a failing element's own IFC PredefinedType is a
    schema-flagged non-default class for its entity type (e.g. IfcSlab
    PredefinedType=ROOF, vs the default FLOOR/NOTDEFINED case). This
    replaced an earlier English-keyword-on-Name heuristic (roof/hollowcore/
    deck/...), which broke for non-English naming and produced a
    coincidentally-right diagnosis on a case (a hollow-core precast plank)
    that turned out to be a labelling fault, not a geometry-measurement bug
    at all. PredefinedType is a real schema enum, not a text guess, so this
    triggers identically regardless of naming language/convention.

None of these classifiers change v3's accept/reject outcome -- v4 is a
better JUDGE, not a different threshold. Use its sharper diagnosis to write
the actual code fix, then re-run to prove the fix (same discipline as the
A2 fix under v2).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

V3_HARNESS_PATH = Path(__file__).resolve().parents[1] / "agentic_pipeline_v3" / "harness.py"


def _load_v3():
    mod_name = "agentic_pipeline_v3_harness"
    spec = importlib.util.spec_from_file_location(mod_name, V3_HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


v3 = _load_v3()
ROOT = v3.ROOT

# IFC schema enums (IfcSlabTypeEnum, IfcRoofTypeEnum-adjacent, etc.) whose
# non-default values imply a shape a simple axis-aligned bounding box is
# unlikely to measure correctly (sloped, stepped, or otherwise non-flat).
# FLOOR/NOTDEFINED/USERDEFINED are left out deliberately -- those are the
# "ordinary prismatic" cases the checker's bbox approach is built for.
_NON_STANDARD_PREDEFINED_TYPES = {
    "IfcSlab": {"ROOF", "LANDING", "BASESLAB"},
}


def classify_degenerate_population(case: "v3.FixtureCase", verdict: str) -> str | None:
    if verdict not in ("not_applicable", "pass"):
        return None
    entity_type = (case.meta or {}).get("element_type")
    if not entity_type or not case.negative_path or not case.negative_path.exists():
        return None
    import ifcopenshell

    baseline_count = len(ifcopenshell.open(str(case.negative_path)).by_type(entity_type))
    fixture_count = len(ifcopenshell.open(str(case.ifc_path)).by_type(entity_type))
    if baseline_count > 0 and fixture_count == 0:
        return (
            f"DEGENERATE_POPULATION: baseline had {baseline_count} {entity_type} element(s); "
            f"this fixture has ZERO. The checker likely bails to '{verdict}' whenever the whole "
            f"population vanishes, instead of treating total loss as a violation in its own right. "
            f"Fix pattern: before returning not_applicable for an empty population, check whether "
            f"the negative/baseline population was non-empty -- if so this is a severe finding, not "
            f"an out-of-scope one."
        )
    return None


def classify_null_ambiguity(case: "v3.FixtureCase") -> str | None:
    meta = case.meta or {}
    before = str(meta.get("before", ""))
    after = str(meta.get("after", ""))
    if before and before.strip().lower() not in ("(no property present)", "none", "") and \
       after.strip().lower() in ("(blank)", "none", ""):
        attr = meta.get("attribute", "the attribute")
        return (
            f"NULL_AMBIGUITY: fixture represents {attr!r} being REMOVED (before={before!r} -> "
            f"after=blank), not 'never designated'. If the checker's null-check treats every blank "
            f"value identically (`if value is None: continue/skip`), it will silently miss this -- "
            f"a removed/downgraded designation is usually ITSELF the violation, distinct from an "
            f"element that never carried the designation at all. Fix pattern: branch explicitly on "
            f"whether the HOST/PARENT context implies the attribute should be present (e.g. the "
            f"element sits in a context where a rating was expected) before treating a blank value "
            f"as out-of-scope."
        )
    return None


def classify_geometry_outlier(case: "v3.FixtureCase") -> str | None:
    meta = case.meta or {}
    entity_type = meta.get("element_type")
    guid = meta.get("global_id")
    flagged_types = _NON_STANDARD_PREDEFINED_TYPES.get(entity_type)
    if not flagged_types or not guid:
        return None

    import ifcopenshell

    try:
        model = ifcopenshell.open(str(case.ifc_path))
        element = model.by_guid(guid)
    except Exception:
        return None

    predefined = getattr(element, "PredefinedType", None)
    if predefined in flagged_types:
        return (
            f"GEOMETRY_OUTLIER: failing element has PredefinedType={predefined!r} -- a schema-"
            f"flagged non-default {entity_type} class (not the ordinary FLOOR/NOTDEFINED case) -- "
            f"bounding-box-derived measurements (thickness, depth, span) are known to be unreliable "
            f"for sloped/non-planar geometry classes like this. Fix pattern: prefer a parametric "
            f"attribute over a bounding-box measurement when one is available, or explicitly exclude "
            f"this PredefinedType from bbox-based measurement and report 'unknown' rather than a "
            f"possibly-wrong pass/fail."
        )
    return None


def enrich(report: "v3.RuleReport") -> dict[str, list[str]]:
    """Run the v4 classifiers over every failing testable case in a v3 report.
    Returns {case_key: [diagnosis, ...]} -- purely additive, does not change
    report.accepted or any FixtureResult.ok value."""
    findings: dict[str, list[str]] = {}
    for r in report.testable:
        if r.ok:
            continue
        key = f"{r.case.building}/{r.case.ifc_path.name}"
        notes = []
        for classifier, args in (
            (classify_degenerate_population, (r.case, r.verdict)),
            (classify_null_ambiguity, (r.case,)),
            (classify_geometry_outlier, (r.case,)),
        ):
            note = classifier(*args)
            if note:
                notes.append(note)
        if notes:
            findings[key] = notes
    return findings


def run_rule(rule_id: str, checker_path: Path, include_multi: bool = True):
    return v3.run_rule(rule_id, checker_path, include_multi=include_multi)


def print_report(report: "v3.RuleReport") -> None:
    v3.print_report(report)
    findings = enrich(report)
    if findings:
        print("\n" + "#" * 78)
        print("### V4 CLASSIFIED DIAGNOSIS (generalized failure classes)")
        print("#" * 78)
        for key, notes in findings.items():
            print(f"\n- {key}")
            for n in notes:
                print(f"    {n}")
