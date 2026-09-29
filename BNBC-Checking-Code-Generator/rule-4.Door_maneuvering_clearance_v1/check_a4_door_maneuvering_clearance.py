"""
Rule A4 — Door manoeuvring clearance obstructed (ANSI A117.1 / ADA).

The full ADA/A117.1 rule is a table of clearance depths/widths that vary
by approach direction (front/hinge/latch) and door side (push/pull) —
reproducing it exactly needs door-swing direction and which side of the
wall is the "approach" side, neither of which is reliably present across
IFC authoring tools (verified in this project: the Dutch export uses a
free-text 'draairichting' property, others have nothing at all).

Convention adopted here (documented simplification, flagged the same
way a spec-card "measurement convention" would be): every door must
have a rectangular clear-floor zone, in plan, of

    CLEARANCE_MARGIN_MM (700 mm, close to the ADA absolute minimum
    maneuvering-clearance figure of 24-32 in depending on approach)

free of solid obstructions on all sides of the door opening, at the
door's own vertical extent. Any IfcFurnishingElement / IfcColumn /
IfcRailing whose bounding box intersects that zone is reported as an
obstruction. This is a conservative, direction-agnostic proxy for the
real rule, not a substitute for the full ADA table.

Usage:
    python check_a4_door_maneuvering_clearance.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.placement as placement_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, geom_settings, get_bbox_mm, length_unit_to_mm  # noqa: E402

RULE_REF = "ANSI A117.1 / ADA 404.2.4 (BNBC 2020 Ch.8 door-clearance analogue)"
CLEARANCE_MARGIN_MM = 700.0
CANDIDATE_PREFILTER_MM = 2500.0
COND = "door_clear_floor_space_obstructed"
OBSTRUCTION_CLASSES = ("IfcFurnishingElement", "IfcColumn", "IfcRailing")


def _origin_mm(element, unit_mm: float):
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        matrix = placement_util.get_local_placement(placement)
        return (matrix[0][3] * unit_mm, matrix[1][3] * unit_mm, matrix[2][3] * unit_mm)
    except Exception:
        return None


def _host_wall_of_door(door):
    for rel in getattr(door, "FillsVoids", None) or []:
        opening = getattr(rel, "RelatingOpeningElement", None)
        if opening is None:
            continue
        for vrel in getattr(opening, "VoidsElements", None) or []:
            host = getattr(vrel, "RelatingBuildingElement", None)
            if host is not None:
                return host
    return None


def _bbox(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    return get_bbox_mm(shape)


def _expand(bbox, margin):
    (x0, y0, z0), (x1, y1, z1) = bbox
    return (x0 - margin, y0 - margin, z0), (x1 + margin, y1 + margin, z1)


def _overlaps(a, b) -> bool:
    (ax0, ay0, az0), (ax1, ay1, az1) = a
    (bx0, by0, bz0), (bx1, by1, bz1) = b
    return ax0 <= bx1 and ax1 >= bx0 and ay0 <= by1 and ay1 >= by0 and az0 <= bz1 and az1 >= bz0


def check_rule(model: ifcopenshell.file) -> dict:
    doors = model.by_type("IfcDoor")
    if not doors:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcDoor elements found in the model.",
            "checks": [],
        }

    unit_mm = length_unit_to_mm(model)
    settings = geom_settings()

    candidates = []
    for cls in OBSTRUCTION_CLASSES:
        candidates.extend(model.by_type(cls))
    candidate_origins = [(_origin_mm(c, unit_mm), c) for c in candidates]
    candidate_origins = [(o, c) for o, c in candidate_origins if o is not None]

    violations = []
    checks = []
    checked = 0
    skipped = 0

    for door in doors:
        door_bbox = _bbox(settings, door)
        if door_bbox is None:
            skipped += 1
            continue
        checked += 1
        zone = _expand(door_bbox, CLEARANCE_MARGIN_MM)
        door_origin = _origin_mm(door, unit_mm)
        host = _host_wall_of_door(door)

        nearby = candidates
        if door_origin is not None:
            dx, dy, dz = door_origin
            nearby = [
                c for (ox, oy, oz), c in candidate_origins
                if abs(ox - dx) <= CANDIDATE_PREFILTER_MM and abs(oy - dy) <= CANDIDATE_PREFILTER_MM
            ]

        threshold = f">= {CLEARANCE_MARGIN_MM:.0f} mm clear on all sides"
        door_obstructions = []
        for obstruction in nearby:
            if host is not None and obstruction.id() == host.id():
                continue
            obs_bbox = _bbox(settings, obstruction)
            if obs_bbox is None:
                continue
            if _overlaps(zone, obs_bbox):
                door_obstructions.append(obstruction)
                violations.append({
                    "condition": COND,
                    "description": "An obstruction intersects the door's required clear floor space.",
                    "rule_ref": RULE_REF,
                    "threshold": threshold,
                    "locations": [{
                        "element": f"{element_label(door)} <- obstructed by {element_label(obstruction)}",
                        "storey": element_storey(door),
                        "measured": f"obstruction={element_label(obstruction)} intersects door clearance zone",
                    }],
                })

        is_violation = bool(door_obstructions)
        if is_violation:
            measured = f"obstruction={element_label(door_obstructions[0])} intersects door clearance zone"
            if len(door_obstructions) > 1:
                measured += f" (+{len(door_obstructions) - 1} more)"
        else:
            measured = "no obstruction intersects door clearance zone"
        checks.append({
            "element": element_label(door),
            "storey": element_storey(door),
            "measured": measured,
            "threshold": threshold,
            "result": "fail" if is_violation else "pass",
        })

    unknown_reasons = []
    if skipped:
        unknown_reasons.append({
            "condition": COND, "missing": "unparseable geometry on IfcDoor",
            "affected_elements": skipped,
        })

    checked_summary = {
        COND: {"elements_checked": checked, "elements_skipped": skipped,
               "skip_reasons": {"no_geometry": skipped} if skipped else {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} door(s) have an obstructed clear floor space."
    elif checked == 0:
        verdict = "unknown"
        summary = "No door has usable geometry; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked door(s) have unobstructed clear floor space (proxy check)."

    return {
        "verdict": verdict, "violations": violations, "violation_count": len(violations),
        "unknown_reasons": unknown_reasons, "checked_summary": checked_summary, "summary": summary,
        "checks": checks,
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
