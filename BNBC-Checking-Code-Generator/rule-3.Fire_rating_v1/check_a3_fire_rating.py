"""
Rule A3 — Fire rating missing or downgraded (IBC Ch.7 / Table 716.1(2)).

Two conditions are checked (matching the two injection patterns the
rule's baseline analysis describes: "downgrade a 2h wall" and "blank
the hosted door"):

  wall_fire_rating_min
      Any IfcWall whose FireRating property IS populated (i.e. it has
      been explicitly designated as a fire-separation wall) must carry
      a rating >= 120 minutes (2 HR). Walls with NO FireRating property
      at all are not evaluated by this condition — that is a *missing
      designation* problem the rule text separately calls out as a
      near-universal baseline defect, not a per-element pass/fail check
      (see the "unknown" reporting instead).

  door_in_rated_wall_needs_rating
      A door that fills an opening in a wall which itself carries a
      FireRating must also carry a non-blank FireRating. A blank rating
      on a door hosted by a rated wall breaks the compartment.

Convention: FireRating values are free-text in IFC ("120", "2 HR",
"120 min", "1.5h" ...). ``_parse_minutes`` normalises common patterns;
values it cannot parse are treated as missing data (unknown), not as
a pass.

Usage:
    python check_a3_fire_rating.py <path-to-ifc>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, property_sets  # noqa: E402

RULE_REF = "IBC Ch.7 / Table 716.1(2) (BNBC 2020 Ch.8 fire-separation analogue)"
MIN_WALL_RATING_MIN = 120.0  # 2 HR
COND_WALL = "wall_fire_rating_min"
COND_WALL_REMOVED = "wall_fire_rating_removed"
COND_DOOR = "door_in_rated_wall_needs_rating"

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_minutes(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    value = float(m.group(1))
    lower = text.lower()
    if "hr" in lower or "hour" in lower or re.search(r"\d\s*h\b", lower):
        return value * 60.0
    return value  # assume minutes


def _fire_rating(element: ifcopenshell.entity_instance, pset_name: str) -> str | None:
    psets = property_sets(element)
    common = psets.get(pset_name, {})
    val = common.get("FireRating")
    if val is None:
        for pname, pdata in psets.items():
            if "FireRating" in pdata:
                val = pdata["FireRating"]
                break
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _fire_rating_key_present(element: ifcopenshell.entity_instance) -> bool:
    """True when a FireRating property KEY exists on the element (even if
    its value is blank). Authoring tools that clear/downgrade a rating
    typically leave the property slot in place with an empty value, while
    an element that was never designated as fire-rated has no such key at
    all -- this distinguishes "rating was removed" (itself a violation)
    from "never applicable" (out of this condition's scope), which a
    bare None-check on the value cannot tell apart."""
    for pdata in property_sets(element).values():
        if "FireRating" in pdata:
            return True
    return False


def _host_wall_of_door(door: ifcopenshell.entity_instance):
    for rel in getattr(door, "FillsVoids", None) or []:
        opening = getattr(rel, "RelatingOpeningElement", None)
        if opening is None:
            continue
        for vrel in getattr(opening, "VoidsElements", None) or []:
            host = getattr(vrel, "RelatingBuildingElement", None)
            if host is not None and host.is_a("IfcWall"):
                return host
    return None


def check_rule(model: ifcopenshell.file) -> dict:
    walls = model.by_type("IfcWall")
    doors = model.by_type("IfcDoor")

    if not walls and not doors:
        return {
            "verdict": "not_applicable",
            "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcWall or IfcDoor elements found in the model.",
            "checks": [],
        }

    violations = []
    checks = []
    wall_checked = wall_skipped = 0
    door_checked = door_skipped = 0

    rated_walls = {}  # wall.id() -> minutes
    for wall in walls:
        raw = _fire_rating(wall, "Pset_WallCommon")
        if raw is None:
            if _fire_rating_key_present(wall):
                # the FireRating slot exists but is blank -- this reads as a
                # rating that was explicitly cleared/removed, not an element
                # that was never designated as fire-rated. Treat the removal
                # itself as the violation (see COND_WALL_REMOVED).
                violations.append({
                    "condition": COND_WALL_REMOVED,
                    "description": "Wall's FireRating designation is blank even though the property slot is present -- reads as a rating that was cleared/removed rather than never applicable.",
                    "rule_ref": RULE_REF,
                    "threshold": "FireRating must remain populated once designated",
                    "locations": [{
                        "element": element_label(wall), "storey": element_storey(wall),
                        "measured": "FireRating=blank (property present but empty)",
                    }],
                })
            continue  # not designated as fire-rated: outside this condition's scope
        minutes = _parse_minutes(raw)
        if minutes is None:
            wall_skipped += 1
            continue
        wall_checked += 1
        rated_walls[wall.id()] = minutes
        wall_measured = f"FireRating={minutes:.0f} min, required>={MIN_WALL_RATING_MIN:.0f} min"
        wall_threshold = f">= {MIN_WALL_RATING_MIN:.0f} min"
        wall_is_violation = minutes < MIN_WALL_RATING_MIN
        checks.append({
            "element": element_label(wall),
            "storey": element_storey(wall),
            "criterion": "Wall fire rating",
            "measured": wall_measured,
            "threshold": wall_threshold,
            "result": "fail" if wall_is_violation else "pass",
        })
        if wall_is_violation:
            violations.append({
                "condition": COND_WALL,
                "description": "Wall fire-resistance rating is below the required minimum.",
                "rule_ref": RULE_REF,
                "threshold": wall_threshold,
                "locations": [{
                    "element": element_label(wall), "storey": element_storey(wall),
                    "measured": wall_measured,
                }],
            })

    for door in doors:
        host = _host_wall_of_door(door)
        if host is None or host.id() not in rated_walls:
            continue  # door not hosted by a rated wall: condition not applicable
        door_checked += 1
        raw = _fire_rating(door, "Pset_DoorCommon")
        door_threshold = "FireRating must be populated"
        door_is_violation = raw is None
        door_measured = (
            f"door FireRating=blank, host wall={rated_walls[host.id()]:.0f} min"
            if door_is_violation
            else f"door FireRating={raw}, host wall={rated_walls[host.id()]:.0f} min"
        )
        checks.append({
            "element": element_label(door),
            "storey": element_storey(door),
            "criterion": "Door fire rating",
            "measured": door_measured,
            "threshold": door_threshold,
            "result": "fail" if door_is_violation else "pass",
        })
        if door_is_violation:
            violations.append({
                "condition": COND_DOOR,
                "description": "Door hosted in a fire-rated wall has no FireRating (blank).",
                "rule_ref": RULE_REF,
                "threshold": door_threshold,
                "locations": [{
                    "element": element_label(door), "storey": element_storey(door),
                    "measured": door_measured,
                }],
            })

    unknown_reasons = []
    if wall_skipped:
        unknown_reasons.append({
            "condition": COND_WALL, "missing": "unparseable FireRating value on IfcWall",
            "affected_elements": wall_skipped,
        })

    checked_summary = {
        COND_WALL: {"elements_checked": wall_checked, "elements_skipped": wall_skipped,
                     "skip_reasons": {"unparseable_FireRating": wall_skipped} if wall_skipped else {}},
        COND_DOOR: {"elements_checked": door_checked, "elements_skipped": door_skipped, "skip_reasons": {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} fire-rating violation(s) found."
    elif wall_checked == 0 and door_checked == 0:
        verdict = "not_applicable"
        summary = "No wall in this model carries an explicit FireRating designation; condition out of scope."
    else:
        verdict = "pass"
        summary = f"All {wall_checked} rated wall(s) and {door_checked} hosted door(s) meet fire-rating requirements."

    return {
        "verdict": verdict,
        "violations": violations,
        "violation_count": len(violations),
        "unknown_reasons": unknown_reasons,
        "checked_summary": checked_summary,
        "summary": summary,
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
