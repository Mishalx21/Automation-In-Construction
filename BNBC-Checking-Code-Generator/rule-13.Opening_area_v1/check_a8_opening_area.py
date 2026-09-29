"""
Rule A8 — Aggregate area of openings for light and ventilation
(BNBC 2020 Part 3 Sec 1.19.6, Table 3.1.12).

Requirement — aggregate area of openings in the exterior wall, excluding
doors, as a percentage of the room's net floor area:

  habitable rooms (sleeping, living, study, dining, ...)   15 %
  kitchens                                                 18 %
  non-habitable spaces (bath, store, staircase, utility)   10 %

Conventions adopted:

* Which opening counts. Sec 1.19.6 counts openings "in the exterior wall,
  excluding doors", so only IfcWindow elements hosted in a wall flagged
  Pset_WallCommon.IsExternal are summed. Doors are excluded even where they
  are glazed, exactly as the clause says.

* Window to room. IfcRelSpaceBoundary almost never links a window to a space
  in real exports, so the link is made through the window's host wall:
  window -> IfcRelFillsElement -> IfcOpeningElement -> IfcRelVoidsElement ->
  wall, then wall -> IfcRelSpaceBoundary -> space. Restricting this to
  EXTERIOR walls is what makes it unambiguous: an exterior wall bounds one
  interior space, so a window is credited to exactly one room.

* Fail-safe on a model that does not carry the link. Where fewer than half
  the model's windows resolve to a host wall, a room-by-room ratio would be
  mostly zeroes and would condemn compliant buildings. The checker reports
  unknown for the whole rule in that case instead of guessing.

* A room that IS bounded by an exterior wall and has no window at all is a
  genuine violation (0 % opening), not missing data — that distinction is
  the reason the exterior-wall test is done separately from the window sum.
  Rooms with no exterior wall are out of scope here; Sec 1.19.5 allows them
  to be served by a ventilation shaft instead.

Usage:
    python check_a8_opening_area.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import (  # noqa: E402
    element_label, element_storey, length_unit_to_mm, property_sets,
)

RULE_ID = "A8"
RULE_REF = "BNBC 2020 Part 3 Sec 1.19.6, Table 3.1.12"
HABITABLE_PERCENT = 15.0
KITCHEN_PERCENT = 18.0
NON_HABITABLE_PERCENT = 10.0
COND_OPENING = "space_min_opening_area_ratio"

# Below this share of windows resolving to a host wall the link is not
# trustworthy enough to report a ratio at all.
MIN_LINK_COVERAGE = 0.5
MIN_PLAUSIBLE_AREA_M2 = 1.0
MAX_PLAUSIBLE_AREA_M2 = 2000.0

_KITCHEN_KEYWORDS = ("kitchen", "keuken", "pantry")
_HABITABLE_KEYWORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ",
    "ward", "waiting", "activity", "lounge", "conference", "meeting", "exam",
    "consult", "operat", "library", "dormitor", "reception", "lab",
    "therapy", "team rm", "break rm", "cubicle", "work station", "workstation",
    "treatment", "clinic", "nurse", "kantoor", "slaapkamer", "woonkamer",
    "eetkamer", "werkkamer", "verblijf",
)
_NON_HABITABLE_KEYWORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower",
    "store", "storage", "stor", "stair", "utility", "utl", "closet",
    "janitor", "jan.", "jan ", "laundry", "corridor", "hallway", "passage",
    "lobby", "vestibule", "circulat", "gang", "overloop", "badkamer",
    "berging", "kast", "trap",
)
# Not an occupiable space at all.
_OUT_OF_SCOPE_KEYWORDS = (
    "shaft", "riser", "chase", "elevator", "elev", "lift", "duct", "roof",
    "void", "open to below", "garage", "parking", "plant", "mechanical",
    "mech", "electrical", "elec", "server", "equip", "instal", "onben",
)


def _space_text(space) -> str:
    return " ".join(
        str(getattr(space, attr, None) or "") for attr in ("LongName", "Name")
    ).lower()


def _required_percent(space) -> float | None:
    """Table 3.1.12 percentage for this space, or None if undecidable."""
    text = _space_text(space)
    if not text.strip():
        return None
    if any(k in text for k in _OUT_OF_SCOPE_KEYWORDS):
        return None
    if any(k in text for k in _KITCHEN_KEYWORDS):
        return KITCHEN_PERCENT
    if any(k in text for k in _NON_HABITABLE_KEYWORDS):
        return NON_HABITABLE_PERCENT
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return HABITABLE_PERCENT
    return None


def _items_of(element, identifier: str):
    representation = getattr(element, "Representation", None)
    if representation is None:
        return []
    out = []
    for rep in getattr(representation, "Representations", None) or []:
        if getattr(rep, "RepresentationIdentifier", None) != identifier:
            continue
        for item in getattr(rep, "Items", None) or []:
            if item.is_a("IfcMappedItem"):
                mapped = item.MappingSource.MappedRepresentation
                out.extend(getattr(mapped, "Items", None) or [])
            else:
                out.append(item)
    return out


def _area_unit_to_m2(model: ifcopenshell.file, unit_mm: float) -> float:
    """Factor converting the project's area unit to m2 (see A7)."""
    prefix_factor = {
        "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1,
        "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6,
    }
    projects = model.by_type("IfcProject")
    units = getattr(getattr(projects[0], "UnitsInContext", None), "Units", None) or [] if projects else []
    for unit in units:
        if getattr(unit, "UnitType", None) != "AREAUNIT":
            continue
        if unit.is_a("IfcSIUnit") and str(getattr(unit, "Name", "")).upper() == "SQUARE_METRE":
            prefix = getattr(unit, "Prefix", None)
            return prefix_factor.get(str(prefix).upper(), 1.0) ** 2 if prefix else 1.0
    return (unit_mm / 1000.0) ** 2


def _polygon_area(points) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _geometry_area_m2(space, unit_mm: float) -> float | None:
    """Plan area of the space's own geometry, in m2."""
    scale = (unit_mm / 1000.0) ** 2
    for item in _items_of(space, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            profile = getattr(item, "SweptArea", None)
            if profile is not None and profile.is_a("IfcRectangleProfileDef"):
                return float(profile.XDim) * float(profile.YDim) * scale
            curve = getattr(profile, "OuterCurve", None) if profile is not None else None
            if curve is not None and curve.is_a("IfcPolyline"):
                return _polygon_area([tuple(p.Coordinates[:2]) for p in curve.Points]) * scale
    for item in _items_of(space, "FootPrint"):
        curves = list(getattr(item, "Elements", None) or []) if item.is_a("IfcGeometricSet") \
            or item.is_a("IfcGeometricCurveSet") else [item]
        best = 0.0
        for curve in curves:
            if curve.is_a("IfcPolyline"):
                best = max(best, _polygon_area([tuple(p.Coordinates[:2]) for p in curve.Points]))
        if best:
            return best * scale
    for item in _items_of(space, "Box"):
        if item.is_a("IfcBoundingBox") and item.XDim and item.YDim:
            return float(item.XDim) * float(item.YDim) * scale
    return None


def _space_area_m2(space, unit_mm: float, area_to_m2: float) -> float | None:
    quantity = None
    for rel in getattr(space, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        quantity_set = getattr(rel, "RelatingPropertyDefinition", None)
        if quantity_set is None or not quantity_set.is_a("IfcElementQuantity"):
            continue
        for item in getattr(quantity_set, "Quantities", None) or []:
            if not item.is_a("IfcQuantityArea") or not getattr(item, "AreaValue", None):
                continue
            value = float(item.AreaValue) * area_to_m2
            if str(getattr(item, "Name", "")) in ("NetFloorArea", "GrossFloorArea"):
                quantity = value
                break
            if quantity is None:
                quantity = value
    if quantity is not None and MIN_PLAUSIBLE_AREA_M2 <= quantity <= MAX_PLAUSIBLE_AREA_M2:
        return quantity
    geometry = _geometry_area_m2(space, unit_mm)
    if geometry is not None and MIN_PLAUSIBLE_AREA_M2 <= geometry <= MAX_PLAUSIBLE_AREA_M2:
        return geometry
    return None


def _host_wall(model, window):
    """window -> opening -> the wall it is cut into."""
    for rel in model.get_inverse(window):
        if not rel.is_a("IfcRelFillsElement"):
            continue
        opening = getattr(rel, "RelatingOpeningElement", None)
        if opening is None:
            continue
        for voids in model.get_inverse(opening):
            if voids.is_a("IfcRelVoidsElement"):
                return getattr(voids, "RelatingBuildingElement", None)
    return None


def _is_external(element) -> bool:
    for pset in property_sets(element).values():
        if pset.get("IsExternal") is True:
            return True
    return False


def _window_area_m2(window, unit_mm: float) -> float | None:
    width = getattr(window, "OverallWidth", None)
    height = getattr(window, "OverallHeight", None)
    if width is None or height is None:
        return None
    return (float(width) * unit_mm / 1000.0) * (float(height) * unit_mm / 1000.0)


def check_rule(model: ifcopenshell.file) -> dict:
    spaces = model.by_type("IfcSpace")
    windows = model.by_type("IfcWindow")

    if not spaces:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcSpace elements found in the model.",
            "checks": [],
        }
    if not windows:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcWindow elements found in the model.",
            "checks": [],
        }

    unit_mm = length_unit_to_mm(model)
    area_to_m2 = _area_unit_to_m2(model, unit_mm)

    # wall GlobalId -> the spaces that wall bounds
    wall_to_spaces: dict[str, list] = {}
    for rel in model.by_type("IfcRelSpaceBoundary"):
        element = getattr(rel, "RelatedBuildingElement", None)
        space = getattr(rel, "RelatingSpace", None)
        if element is None or space is None or not element.is_a("IfcWall"):
            continue
        wall_to_spaces.setdefault(element.GlobalId, []).append(space)

    # space GlobalId -> summed exterior-window area, and whether it has an
    # exterior wall at all
    opening_area: dict[str, float] = {}
    missing_window_size: dict[str, int] = {}
    linked_windows = 0
    for window in windows:
        wall = _host_wall(model, window)
        if wall is None:
            continue
        linked_windows += 1
        if not _is_external(wall):
            continue
        area_m2 = _window_area_m2(window, unit_mm)
        for space in wall_to_spaces.get(wall.GlobalId, ()):
            if area_m2 is None:
                missing_window_size[space.GlobalId] = missing_window_size.get(space.GlobalId, 0) + 1
            else:
                opening_area[space.GlobalId] = opening_area.get(space.GlobalId, 0.0) + area_m2

    coverage = linked_windows / len(windows)
    if coverage < MIN_LINK_COVERAGE:
        return {
            "verdict": "unknown", "violations": [], "violation_count": 0,
            "unknown_reasons": [{
                "condition": COND_OPENING,
                "missing": "IfcRelFillsElement linking IfcWindow to its host wall",
                "affected_elements": len(windows) - linked_windows,
            }],
            "checked_summary": {COND_OPENING: {
                "elements_checked": 0, "elements_skipped": len(spaces),
                "skip_reasons": {"window_host_wall_link_unusable": len(spaces)},
            }},
            "summary": (
                f"Only {linked_windows} of {len(windows)} window(s) resolve to a host wall "
                f"({coverage:.0%}); the window-to-room link is not reliable enough to "
                f"measure opening ratios."
            ),
            "checks": [],
        }

    spaces_with_external_wall = set()
    for wall_gid, bounded in wall_to_spaces.items():
        try:
            wall = model.by_guid(wall_gid)
        except Exception:
            continue
        if _is_external(wall):
            spaces_with_external_wall.update(s.GlobalId for s in bounded)

    violations = []
    checks = []
    checked = 0
    unclassified = 0
    no_area = 0
    no_exterior_wall = 0
    unreliable = 0

    for space in spaces:
        required_percent = _required_percent(space)
        if required_percent is None:
            unclassified += 1
            continue
        if space.GlobalId not in spaces_with_external_wall:
            no_exterior_wall += 1
            continue
        if missing_window_size.get(space.GlobalId):
            unreliable += 1
            continue

        floor_area_m2 = _space_area_m2(space, unit_mm, area_to_m2)
        if floor_area_m2 is None:
            no_area += 1
            continue

        window_area_m2 = opening_area.get(space.GlobalId, 0.0)
        # Rounded so float noise from the unit conversion cannot report an
        # exactly-compliant ratio as a violation.
        ratio_percent = round(100.0 * window_area_m2 / floor_area_m2, 3)
        checked += 1

        measured = (
            f"opening_ratio={ratio_percent:.1f}%, required={required_percent:.0f}%, "
            f"opening_area={window_area_m2:.2f} m2, floor_area={floor_area_m2:.2f} m2"
        )
        threshold = f">= {required_percent:.0f}% of net floor area"
        is_violation = ratio_percent < required_percent
        checks.append({
            "element": element_label(space),
            "storey": element_storey(space),
            "measured": measured,
            "threshold": threshold,
            "result": "fail" if is_violation else "pass",
        })

        if is_violation:
            violations.append({
                "condition": COND_OPENING,
                "description": (
                    "Aggregate area of openings in the exterior wall is below the "
                    "minimum percentage of net floor area."
                ),
                "rule_ref": RULE_REF,
                "threshold": threshold,
                "locations": [{
                    "element": element_label(space),
                    "storey": element_storey(space),
                    "measured": measured,
                }],
            })

    unknown_reasons = []
    if unclassified:
        unknown_reasons.append({
            "condition": COND_OPENING,
            "missing": "Name/LongName identifying the space's use in Table 3.1.12",
            "affected_elements": unclassified,
        })
    if no_area:
        unknown_reasons.append({
            "condition": COND_OPENING,
            "missing": "net floor area quantity or usable geometry on IfcSpace",
            "affected_elements": no_area,
        })
    if unreliable:
        unknown_reasons.append({
            "condition": COND_OPENING,
            "missing": "OverallWidth/OverallHeight on a window serving the space",
            "affected_elements": unreliable,
        })

    skip_reasons = {
        k: v for k, v in (
            ("unclassified_space", unclassified),
            ("no_exterior_wall", no_exterior_wall),
            ("missing_floor_area", no_area),
            ("window_without_size", unreliable),
        ) if v
    }
    checked_summary = {COND_OPENING: {
        "elements_checked": checked,
        "elements_skipped": len(spaces) - checked,
        "skip_reasons": skip_reasons,
    }}

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} of {checked} checked space(s) have less opening area than "
            f"Table 3.1.12 requires."
        )
    elif checked == 0:
        verdict = "unknown" if (unclassified or no_area or unreliable) else "not_applicable"
        summary = (
            "No space could be measured against Table 3.1.12."
            if verdict == "unknown"
            else "No space governed by Sec 1.19.6 is bounded by an exterior wall."
        )
    else:
        verdict = "pass"
        summary = f"All {checked} checked space(s) meet the Table 3.1.12 opening percentage."

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
