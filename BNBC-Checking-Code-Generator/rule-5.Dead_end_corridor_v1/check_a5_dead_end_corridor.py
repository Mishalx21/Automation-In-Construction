"""
Rule A5 — Dead-end corridor or exit separation (IBC 1020.4 / 1007.1.1).

The real rule needs a full space-adjacency graph and travel-distance
routing to determine dead ends and a floor-plan diagonal to determine
required exit separation. IFC data rarely carries an explicit
space-to-space connectivity graph, so this checker builds a proxy graph
from door <-> space geometric adjacency (a door "connects" a space when
the door's placement origin falls inside that space's bounding box,
expanded by a small margin). This is the same kind of interpretation
gap the rule's own baseline notes flag as the "hardest architectural
case" — a real spec card would mark this an open ambiguity pending
human adjudication rather than a fully mechanical check.

Two conditions:

  corridor_dead_end_length
      An IfcSpace whose Name/LongName suggests a corridor, reachable
      through only one door (a dead end), with a plan length greater
      than DEAD_END_MAX_MM (6100 mm / 20 ft per IBC 1020.4).

  exit_separation_min
      Two or more external, ground-storey doors (candidate exits) must
      be at least half the storey's plan diagonal apart (IBC 1007.1.1,
      non-sprinklered case).

Usage:
    python check_a5_dead_end_corridor.py <path-to-ifc>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.placement as placement_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import (  # noqa: E402
    element_label, element_storey, geom_settings, get_bbox_mm, length_unit_to_mm, property_sets,
)

RULE_REF = "IBC 1020.4 / 1007.1.1 (BNBC 2020 Ch.8 egress-topology analogue)"
DEAD_END_MAX_MM = 6100.0
ADJACENCY_MARGIN_MM = 300.0
COND_DEAD_END = "corridor_dead_end_length"
COND_EXIT_SEP = "exit_separation_min"
_CORRIDOR_KEYWORDS = ("corridor", "hall", "gang", "passage", "hallway")


def _origin_mm(element, unit_mm: float):
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        matrix = placement_util.get_local_placement(placement)
        return (matrix[0][3] * unit_mm, matrix[1][3] * unit_mm, matrix[2][3] * unit_mm)
    except Exception:
        return None


def _bbox(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    return get_bbox_mm(shape)


def _is_external(door) -> bool:
    common = property_sets(door).get("Pset_DoorCommon", {})
    return bool(common.get("IsExternal"))


def _point_in_bbox(point, bbox, margin) -> bool:
    if point is None or bbox is None:
        return False
    (x0, y0, z0), (x1, y1, z1) = bbox
    px, py, pz = point
    return (x0 - margin) <= px <= (x1 + margin) and (y0 - margin) <= py <= (y1 + margin)


def check_rule(model: ifcopenshell.file) -> dict:
    spaces = model.by_type("IfcSpace")
    doors = model.by_type("IfcDoor")

    if not spaces and not doors:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcSpace or IfcDoor elements found in the model.",
        }

    unit_mm = length_unit_to_mm(model)
    settings = geom_settings()

    violations = []
    unknown_reasons = []

    # ---- condition 1: corridor dead ends ---------------------------------
    corridors = [
        s for s in spaces
        if any(k in ((s.Name or "") + " " + (s.LongName or "")).lower() for k in _CORRIDOR_KEYWORDS)
    ]
    door_origins = [(d, _origin_mm(d, unit_mm)) for d in doors]
    door_origins = [(d, o) for d, o in door_origins if o is not None]

    corridor_checked = 0
    corridor_skipped = 0
    for space in corridors:
        bbox = _bbox(settings, space)
        if bbox is None:
            corridor_skipped += 1
            continue
        corridor_checked += 1
        connected = [d for d, o in door_origins if _point_in_bbox(o, bbox, ADJACENCY_MARGIN_MM)]
        (x0, y0, _), (x1, y1, _) = bbox
        plan_length = max(x1 - x0, y1 - y0)
        if len(connected) <= 1 and plan_length > DEAD_END_MAX_MM:
            violations.append({
                "condition": COND_DEAD_END,
                "description": "Corridor is a dead end (<=1 exit door) longer than the allowed maximum.",
                "rule_ref": RULE_REF,
                "threshold": f"<= {DEAD_END_MAX_MM:.0f} mm when only one door serves the corridor",
                "locations": [{
                    "element": element_label(space), "storey": element_storey(space),
                    "measured": f"length={plan_length:.0f} mm, connected_doors={len(connected)}",
                }],
            })

    if corridor_skipped:
        unknown_reasons.append({
            "condition": COND_DEAD_END, "missing": "unparseable geometry on IfcSpace (corridor)",
            "affected_elements": corridor_skipped,
        })

    # ---- condition 2: exit separation --------------------------------------
    exit_doors = [d for d in doors if _is_external(d)]
    exit_points = [(d, o) for d, o in door_origins if d in exit_doors]

    exit_checked = len(exit_points)
    if len(exit_points) >= 2:
        ground_spaces_bbox = None
        for s in spaces:
            bbox = _bbox(settings, s)
            if bbox is None:
                continue
            if ground_spaces_bbox is None:
                ground_spaces_bbox = bbox
            else:
                (x0, y0, z0), (x1, y1, z1) = ground_spaces_bbox
                (nx0, ny0, nz0), (nx1, ny1, nz1) = bbox
                ground_spaces_bbox = (
                    (min(x0, nx0), min(y0, ny0), min(z0, nz0)),
                    (max(x1, nx1), max(y1, ny1), max(z1, nz1)),
                )
        if ground_spaces_bbox is not None:
            (x0, y0, _), (x1, y1, _) = ground_spaces_bbox
            diagonal = math.hypot(x1 - x0, y1 - y0)
            required_min = diagonal / 2.0

            max_dist = 0.0
            worst_pair = None
            for i in range(len(exit_points)):
                for j in range(i + 1, len(exit_points)):
                    d1, o1 = exit_points[i]
                    d2, o2 = exit_points[j]
                    dist = math.hypot(o1[0] - o2[0], o1[1] - o2[1])
                    if dist > max_dist:
                        max_dist = dist
                        worst_pair = (d1, d2)

            if worst_pair is not None and max_dist < required_min:
                d1, d2 = worst_pair
                violations.append({
                    "condition": COND_EXIT_SEP,
                    "description": "The two most widely separated exits are closer than half the plan diagonal.",
                    "rule_ref": RULE_REF,
                    "threshold": f">= {required_min:.0f} mm (half of {diagonal:.0f} mm diagonal)",
                    "locations": [{
                        "element": f"{element_label(d1)} <-> {element_label(d2)}",
                        "storey": element_storey(d1),
                        "measured": f"separation={max_dist:.0f} mm, required>={required_min:.0f} mm",
                    }],
                })
        else:
            unknown_reasons.append({
                "condition": COND_EXIT_SEP, "missing": "no IfcSpace geometry to derive plan diagonal",
                "affected_elements": 0,
            })
    elif len(exit_points) == 1:
        unknown_reasons.append({
            "condition": COND_EXIT_SEP, "missing": "fewer than 2 external doors found (IsExternal=True)",
            "affected_elements": 1,
        })

    checked_summary = {
        COND_DEAD_END: {"elements_checked": corridor_checked, "elements_skipped": corridor_skipped,
                         "skip_reasons": {"no_geometry": corridor_skipped} if corridor_skipped else {}},
        COND_EXIT_SEP: {"elements_checked": exit_checked, "elements_skipped": 0, "skip_reasons": {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} egress-topology violation(s) found (dead-end corridor / exit separation)."
    elif corridor_checked == 0 and exit_checked < 2:
        verdict = "unknown"
        summary = "Insufficient corridor/exit-door data to evaluate dead-end or exit-separation conditions."
    else:
        verdict = "pass"
        summary = "No dead-end corridor or exit-separation violation detected (proxy check)."

    return {
        "verdict": verdict, "violations": violations, "violation_count": len(violations),
        "unknown_reasons": unknown_reasons, "checked_summary": checked_summary, "summary": summary,
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
