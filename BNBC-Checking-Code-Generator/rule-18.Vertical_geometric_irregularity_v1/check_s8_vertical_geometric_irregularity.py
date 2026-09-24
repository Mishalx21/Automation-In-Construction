"""
Rule S8 — Vertical geometric irregularity (setback)
(BNBC 2020 Part 6 Table 6.1.4, Vertical Irregularity Type III).

Requirement: "Vertical geometric irregularity shall be considered to exist
where horizontal dimension of the lateral force-resisting system in any
storey is more than 130 percent of that in an adjacent storey; one-storey
penthouses need not be considered."

Conventions adopted:

* The lateral force-resisting system. IFC carries no analysis model, so the
  LFRS is taken to be the vertical elements that can carry lateral load:
  IfcColumn plus IfcWall. Where any wall in the model declares
  Pset_WallCommon.LoadBearing, only load-bearing walls are counted;
  otherwise every wall is used as a proxy, the same fallback the soft-storey
  rule (S5) applies to the same data.

* Horizontal dimension. Measured per storey as the extent of its LFRS
  element placements along each global axis, and compared axis by axis. Plan
  extents, not floor areas, are what the clause names, and measuring from
  placement origins rather than tessellated solids keeps the check cheap on
  a large model. A storey's extent is the distance between its outermost
  columns/walls, so it understates the true plan dimension by roughly one
  column offset on each side — an error that largely cancels in the ratio
  between two storeys.

* Penthouses. The clause exempts one-storey penthouses. The topmost storey
  is exempted when it carries a small fraction of the building's typical
  LFRS element count, which is the IFC-observable signature of a penthouse
  or a roof plant enclosure.

* Comparability. A plan dimension measured from a handful of elements is
  not a plan dimension. A storey is compared only when it carries at least
  MIN_ELEMENTS_PER_STOREY lateral elements AND at least
  MIN_COMPARABLE_FRACTION of the building's median storey count. Without
  this a sparsely modelled level — one real model in this corpus has 6
  elements on a storey where the median is 39 — reads as a 6:1 setback that
  does not exist. Pairs where either storey falls short are reported as
  skipped, never as compliant.

* Adjacency. Only neighbouring storeys are compared, as the clause says.
  Storeys carrying no lateral element at all are left out of the stack
  first — a footing datum or a "top of steel" reference level is not a
  storey of the lateral system, and leaving it in would break the adjacency
  of the real storeys on either side. Among the rest, when an intermediate
  storey is not comparable the pairs it belongs to are skipped rather than
  silently bridged across it.

Usage:
    python check_s8_vertical_geometric_irregularity.py <path-to-ifc>
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.placement as placement_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import length_unit_to_mm, property_sets  # noqa: E402

RULE_ID = "S8"
RULE_REF = "BNBC 2020 Part 6 Table 6.1.4, Vertical Irregularity Type III"
MAX_DIMENSION_RATIO = 1.30
COND_SETBACK = "storey_plan_dimension_jump"

MIN_ELEMENTS_PER_STOREY = 3
# A storey holding far fewer lateral elements than the building's median is
# sparsely modelled rather than set back, and its extent is not comparable.
MIN_COMPARABLE_FRACTION = 0.4
# A top storey holding less than this share of the typical storey's element
# count is the penthouse the clause sets aside.
PENTHOUSE_ELEMENT_FRACTION = 0.25
# Below this a "plan dimension" is a single bay, not a building footprint.
MIN_PLAUSIBLE_EXTENT_MM = 1000.0


def _is_load_bearing(wall) -> bool | None:
    value = property_sets(wall).get("Pset_WallCommon", {}).get("LoadBearing")
    return None if value is None else bool(value)


def _plan_xy_mm(element, unit_mm: float):
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        matrix = placement_util.get_local_placement(placement)
    except Exception:
        return None
    return float(matrix[0][3]) * unit_mm, float(matrix[1][3]) * unit_mm


def _storey_of(model, element):
    for rel in model.get_inverse(element):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            structure = getattr(rel, "RelatingStructure", None)
            if structure is not None and structure.is_a("IfcBuildingStorey"):
                return structure
    return None


def check_rule(model: ifcopenshell.file) -> dict:
    storeys = [s for s in model.by_type("IfcBuildingStorey") if s.Elevation is not None]
    columns = list(model.by_type("IfcColumn"))
    walls = list(model.by_type("IfcWall"))

    if len(storeys) < 2:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": (
                "Fewer than two IfcBuildingStorey elements carry an elevation; "
                "there is no adjacent storey to compare against."
            ),
        }
    if not columns and not walls:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcColumn or IfcWall elements to stand for the lateral force-resisting system.",
        }

    unit_mm = length_unit_to_mm(model)

    wall_flags = [_is_load_bearing(w) for w in walls]
    any_flagged = any(flag is True for flag in wall_flags)
    lfrs = list(columns) + [
        wall for wall, flag in zip(walls, wall_flags) if not any_flagged or flag is True
    ]
    wall_basis = (
        "load-bearing walls only" if any_flagged else "all walls (no LoadBearing flag in model)"
    )

    # storey GlobalId -> plan points of its LFRS elements
    points: dict[str, list[tuple[float, float]]] = {}
    unplaced = 0
    for element in lfrs:
        storey = _storey_of(model, element)
        if storey is None or storey.Elevation is None:
            unplaced += 1
            continue
        xy = _plan_xy_mm(element, unit_mm)
        if xy is None:
            unplaced += 1
            continue
        points.setdefault(storey.GlobalId, []).append(xy)

    # Only storeys that actually carry lateral elements are part of the
    # lateral system's stack. A storey holding none of them (a footing
    # datum, a "top of steel" reference level) is not a storey of the LFRS
    # and must not break the adjacency of the storeys around it. A storey
    # that lost its lateral elements is the soft-storey case, which S5 is
    # the rule for.
    ordered = sorted(
        (s for s in storeys if points.get(s.GlobalId)),
        key=lambda s: (s.Elevation, s.GlobalId),
    )
    counts = {s.GlobalId: len(points.get(s.GlobalId, ())) for s in ordered}
    populated = [c for c in counts.values() if c]
    if len(populated) < 2:
        return {
            "verdict": "unknown", "violations": [], "violation_count": 0,
            "unknown_reasons": [{
                "condition": COND_SETBACK,
                "missing": "lateral elements placed on at least two storeys",
                "affected_elements": unplaced or len(lfrs),
            }],
            "checked_summary": {COND_SETBACK: {
                "elements_checked": 0, "elements_skipped": len(ordered),
                "skip_reasons": {"no_lateral_elements_on_storey": len(ordered)},
            }},
            "summary": (
                "Lateral elements resolve to fewer than two storeys; no adjacent pair "
                "can be compared."
            ),
        }

    median_count = statistics.median(populated)
    exempt: set[str] = set()
    # One-storey penthouse: the top storey only, and only when it is sparse.
    top = ordered[-1]
    if counts[top.GlobalId] and counts[top.GlobalId] < median_count * PENTHOUSE_ELEMENT_FRACTION:
        exempt.add(top.GlobalId)

    min_comparable = max(MIN_ELEMENTS_PER_STOREY, median_count * MIN_COMPARABLE_FRACTION)
    extents: dict[str, tuple[float, float]] = {}
    too_few = 0
    for storey in ordered:
        if storey.GlobalId in exempt:
            continue
        storey_points = points.get(storey.GlobalId, [])
        if len(storey_points) < min_comparable:
            if storey_points:
                too_few += 1
            continue
        xs = [p[0] for p in storey_points]
        ys = [p[1] for p in storey_points]
        extents[storey.GlobalId] = (
            round(max(xs) - min(xs), 1),
            round(max(ys) - min(ys), 1),
        )

    comparable = [s for s in ordered if s.GlobalId in extents]
    if len(comparable) < 2:
        return {
            "verdict": "unknown", "violations": [], "violation_count": 0,
            "unknown_reasons": [{
                "condition": COND_SETBACK,
                "missing": f"at least {MIN_ELEMENTS_PER_STOREY} placed lateral elements on two storeys",
                "affected_elements": len(ordered),
            }],
            "checked_summary": {COND_SETBACK: {
                "elements_checked": len(comparable), "elements_skipped": len(ordered) - len(comparable),
                "skip_reasons": {"too_few_lateral_elements": too_few or len(ordered)},
            }},
            "summary": (
                f"Fewer than two storeys carry at least {MIN_ELEMENTS_PER_STOREY} placed "
                f"lateral elements; the plan dimension cannot be compared."
            ),
        }

    violations = []
    compared_pairs = 0
    skipped_pairs = 0
    # Neighbours in the FULL storey order, so an excluded intermediate storey
    # never lets two non-adjacent storeys be compared to each other.
    for lower, upper in zip(ordered, ordered[1:]):
        if lower.GlobalId not in extents or upper.GlobalId not in extents:
            skipped_pairs += 1
            continue
        compared_pairs += 1
        for axis, index in (("X", 0), ("Y", 1)):
            a = extents[lower.GlobalId][index]
            b = extents[upper.GlobalId][index]
            if min(a, b) < MIN_PLAUSIBLE_EXTENT_MM:
                continue
            larger, smaller = (a, b) if a >= b else (b, a)
            ratio = larger / smaller
            if ratio <= MAX_DIMENSION_RATIO:
                continue
            wider = lower if a >= b else upper
            narrower = upper if a >= b else lower
            violations.append({
                "condition": COND_SETBACK,
                "description": (
                    "Plan dimension of the lateral force-resisting system changes by more "
                    "than 130% between adjacent storeys — a vertical geometric irregularity."
                ),
                "rule_ref": RULE_REF,
                "threshold": f"<= {MAX_DIMENSION_RATIO:.0%} of the adjacent storey",
                # The irregularity belongs to the PAIR, not to one level: the
                # plan steps between them. Both storeys are reported so the
                # finding can be found from either end of the step - looking
                # only at the wider one hides it from anyone inspecting the
                # storey that was actually set back.
                "locations": [
                    {
                        "element": f"Storey '{wider.Name or wider.GlobalId}'",
                        "storey": str(wider.Name or wider.GlobalId),
                        "measured": (
                            f"axis={axis}, dimension={larger:.0f} mm, "
                            f"adjacent='{narrower.Name or narrower.GlobalId}' at {smaller:.0f} mm, "
                            f"ratio={ratio:.2f}, limit={MAX_DIMENSION_RATIO:.2f}, "
                            f"role=wider, basis={wall_basis}"
                        ),
                    },
                    {
                        "element": f"Storey '{narrower.Name or narrower.GlobalId}'",
                        "storey": str(narrower.Name or narrower.GlobalId),
                        "measured": (
                            f"axis={axis}, dimension={smaller:.0f} mm, "
                            f"adjacent='{wider.Name or wider.GlobalId}' at {larger:.0f} mm, "
                            f"ratio={ratio:.2f}, limit={MAX_DIMENSION_RATIO:.2f}, "
                            f"role=narrower, basis={wall_basis}"
                        ),
                    },
                ],
            })

    unknown_reasons = []
    if unplaced:
        unknown_reasons.append({
            "condition": COND_SETBACK,
            "missing": "storey containment or resolvable placement on a lateral element",
            "affected_elements": unplaced,
        })

    if too_few:
        unknown_reasons.append({
            "condition": COND_SETBACK,
            "missing": (
                "enough lateral elements on the storey to measure a plan dimension "
                f"(at least {min_comparable:.0f})"
            ),
            "affected_elements": too_few,
        })

    checked_summary = {COND_SETBACK: {
        "elements_checked": compared_pairs,
        "elements_skipped": skipped_pairs,
        "skip_reasons": {
            k: v for k, v in (
                ("storey_not_comparable", too_few),
                ("penthouse_exempt", len(exempt)),
            ) if v
        },
    }}

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} adjacent-storey pair(s) differ in plan dimension by more "
            f"than {MAX_DIMENSION_RATIO:.0%}."
        )
    elif compared_pairs == 0:
        verdict = "unknown"
        summary = (
            f"No two adjacent storeys both carry enough lateral elements to measure a "
            f"plan dimension; {skipped_pairs} storey pair(s) were skipped."
        )
    else:
        verdict = "pass"
        summary = (
            f"All {compared_pairs} adjacent storey pair(s) stay within "
            f"{MAX_DIMENSION_RATIO:.0%} in both plan directions."
        )

    return {
        "verdict": verdict,
        "violations": violations,
        "violation_count": len(violations),
        "unknown_reasons": unknown_reasons,
        "checked_summary": checked_summary,
        "summary": summary,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(__file__).name} <path-to-ifc>")
        return 2

    model = ifcopenshell.open(sys.argv[1])
    result = check_rule(model)

    print(json.dumps(result, indent=2))
    print(f"\nVerdict: {result['verdict'].upper()}")
    for v in result["violations"]:
        for loc in v["locations"]:
            print(f"  - [{v['condition']}] {loc['element']} @ {loc['storey']}: {loc['measured']}")

    return 1 if result["verdict"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
