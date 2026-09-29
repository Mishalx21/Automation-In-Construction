"""
Rule A7 — Minimum room size: floor area and least width
(BNBC 2020 Part 3 Sec 1.14.2.2).

Requirement:
  habitable room        net floor area >= 9.5 m2  and least width >= 2.9 m
  other (non-habitable) net floor area >= 5.0 m2  and least width >= 2.0 m

Conventions adopted:

* Scope. Sec 1.14.2.2 is written for rooms of a dwelling unit. IFC carries
  no BNBC occupancy classification, so the limits are applied to every
  recognisable room. On a non-residential model the result is therefore an
  indicative screen against the residential minimum rather than a literal
  determination — the same occupancy gap A6 documents.

* Area. Read from a NetFloorArea / GrossFloorArea / Area quantity on the
  space, converted using the project's declared AREAUNIT (which is not the
  square of the LENGTHUNIT in several models of this corpus), and falling
  back to the plan area of the space's own geometry — an extruded profile,
  a Brep, a FootPrint curve or, last, a bounding box. A quantity outside a
  plausible room range is distrusted and the geometry used instead, because
  some exporters write a "reduced" area that is not the room's floor area.

* Least width. Measured as the shorter side of the space footprint's
  bounding rectangle in the space's own local axes. For a rectangular room
  that is exact; for an L-shaped or splayed room the bounding rectangle is
  wider than the true least width, so this measure can only under-report a
  violation, never invent one.

* Spaces that are not classifiable, or carry neither an area quantity nor
  usable geometry, are reported unknown rather than passed.

Usage:
    python check_a7_room_size.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, length_unit_to_mm  # noqa: E402

RULE_ID = "A7"
RULE_REF = "BNBC 2020 Part 3 Sec 1.14.2.2"
MIN_HABITABLE_AREA_M2 = 9.5
MIN_HABITABLE_WIDTH_MM = 2900.0
MIN_OTHER_AREA_M2 = 5.0
MIN_OTHER_WIDTH_MM = 2000.0
COND_AREA = "room_min_floor_area"
COND_WIDTH = "room_min_least_width"

# Anything outside this band is a shaft, a duct, a plenum, an "open to
# below" placeholder or a whole-floor zone, not a room the clause governs.
MIN_PLAUSIBLE_AREA_M2 = 1.0
MAX_PLAUSIBLE_AREA_M2 = 2000.0

_CORRIDOR_KEYWORDS = (
    "corridor", "hallway", "passage", "lobby", "vestibule", "circulat",
    "gang", "overloop", "hal ", "vest", "entry", "entrance", "entree",
)
_HABITABLE_KEYWORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ",
    "ward", "waiting", "activity", "lounge", "conference", "meeting", "exam",
    "consult", "operat", "library", "dormitor", "reception", "lab",
    "therapy", "team rm", "break rm", "cubicle", "work station", "workstation",
    "treatment", "clinic", "nurse", "kantoor", "slaapkamer", "woonkamer",
    "eetkamer", "werkkamer", "verblijf",
)
_OTHER_ROOM_KEYWORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower",
    "store", "storage", "stor", "kitchen", "pantry", "laundry", "utility",
    "utl", "closet", "janitor", "jan.", "jan ", "badkamer", "keuken",
    "berging", "kast",
)
# Never a "room" in the sense of Sec 1.14.2.2.
_OUT_OF_SCOPE_KEYWORDS = (
    "stair", "shaft", "riser", "chase", "elevator", "elev", "lift", "duct",
    "roof", "void", "open to below", "garage", "parking", "plant",
    "mechanical", "mech", "electrical", "elec", "server", "tele", "comm.",
    "comm rm", "equip", "instal", "trap", "meterkast", "onben",
)


def _space_text(space) -> str:
    return " ".join(
        str(getattr(space, attr, None) or "") for attr in ("LongName", "Name")
    ).lower()


def _classify(space) -> str | None:
    """'habitable' | 'other' | 'out_of_scope' | None (undecidable)."""
    text = _space_text(space)
    if not text.strip():
        return None
    if any(k in text for k in _CORRIDOR_KEYWORDS):
        return "out_of_scope"
    if any(k in text for k in _OUT_OF_SCOPE_KEYWORDS):
        return "out_of_scope"
    if any(k in text for k in _OTHER_ROOM_KEYWORDS):
        return "other"
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return "habitable"
    return None


def _items_of(element, identifier: str):
    """Items of one representation, following IfcMappedItem one level."""
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
    """Factor converting the project's area unit to square metres.

    A model's AREAUNIT is declared independently of its LENGTHUNIT — every
    model in this corpus is SQUARE_METRE even where lengths are millimetres —
    so squaring the length scale is wrong. The squared length scale is used
    only when no AREAUNIT is declared at all.
    """
    prefix_factor = {
        "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
        "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2,
        "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
    }
    projects = model.by_type("IfcProject")
    units = getattr(getattr(projects[0], "UnitsInContext", None), "Units", None) or [] if projects else []
    for unit in units:
        if getattr(unit, "UnitType", None) != "AREAUNIT":
            continue
        if unit.is_a("IfcSIUnit"):
            if str(getattr(unit, "Name", "")).upper() != "SQUARE_METRE":
                continue
            prefix = getattr(unit, "Prefix", None)
            # An area prefix scales the underlying length, hence squared.
            return prefix_factor.get(str(prefix).upper(), 1.0) ** 2 if prefix else 1.0
        if unit.is_a("IfcConversionBasedUnit"):
            factor = getattr(unit, "ConversionFactor", None)
            value = getattr(factor, "ValueComponent", None) if factor is not None else None
            if value is not None:
                return float(getattr(value, "wrappedValue", value))
    return (unit_mm / 1000.0) ** 2


def _profile_points(profile):
    """Outer-boundary points of a profile, in the profile's own 2D axes."""
    if profile is None:
        return []
    if profile.is_a("IfcRectangleProfileDef"):
        x, y = float(profile.XDim) / 2.0, float(profile.YDim) / 2.0
        return [(-x, -y), (x, -y), (x, y), (-x, y)]
    curve = getattr(profile, "OuterCurve", None) or getattr(profile, "Curve", None)
    points = []
    if curve is None:
        return points
    if curve.is_a("IfcPolyline"):
        points = [tuple(p.Coordinates[:2]) for p in curve.Points]
    elif curve.is_a("IfcCompositeCurve"):
        for segment in getattr(curve, "Segments", None) or []:
            parent = getattr(segment, "ParentCurve", None)
            if parent is not None and parent.is_a("IfcPolyline"):
                points.extend(tuple(p.Coordinates[:2]) for p in parent.Points)
    return points


def _polygon_area(points) -> float:
    """Shoelace area of a closed ring, in the points' own units."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _ring_metrics(points, unit_mm: float):
    """(area_m2, least_width_mm) for a closed plan ring, or None."""
    if len(points) < 3:
        return None
    area_m2 = _polygon_area(points) * (unit_mm / 1000.0) ** 2
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    width_mm = min(max(xs) - min(xs), max(ys) - min(ys)) * unit_mm
    return area_m2, width_mm


def _footprint(space, unit_mm: float):
    """(area_m2, least_width_mm) from the space's own geometry, or None."""
    for item in _items_of(space, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            points = _profile_points(getattr(item, "SweptArea", None))
            if len(points) >= 3:
                area_m2 = _polygon_area(points) * (unit_mm / 1000.0) ** 2
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                width_mm = min(max(xs) - min(xs), max(ys) - min(ys)) * unit_mm
                return area_m2, width_mm
        if item.is_a("IfcFacetedBrep"):
            xs, ys = [], []
            shell = getattr(item, "Outer", None)
            for face in getattr(shell, "CfsFaces", None) or []:
                for bound in getattr(face, "Bounds", None) or []:
                    loop = getattr(bound, "Bound", None)
                    if loop is not None and loop.is_a("IfcPolyLoop"):
                        for point in loop.Polygon:
                            xs.append(point.Coordinates[0])
                            ys.append(point.Coordinates[1])
            if xs and ys:
                dx = (max(xs) - min(xs)) * unit_mm
                dy = (max(ys) - min(ys)) * unit_mm
                # A Brep gives no boundary ring to integrate, so the plan
                # bounding rectangle stands in for the area as well.
                return (dx / 1000.0) * (dy / 1000.0), min(dx, dy)

    # ArchiCAD-style exports give a space no Body at all — only a plan
    # FootPrint curve, which is the exact room boundary, and a Box.
    for item in _items_of(space, "FootPrint"):
        rings = []
        if item.is_a("IfcGeometricCurveSet") or item.is_a("IfcGeometricSet"):
            rings = list(getattr(item, "Elements", None) or [])
        else:
            rings = [item]
        best = None
        for curve in rings:
            points = []
            if curve.is_a("IfcPolyline"):
                points = [tuple(p.Coordinates[:2]) for p in curve.Points]
            elif curve.is_a("IfcCompositeCurve"):
                for segment in getattr(curve, "Segments", None) or []:
                    parent = getattr(segment, "ParentCurve", None)
                    if parent is not None and parent.is_a("IfcPolyline"):
                        points.extend(tuple(p.Coordinates[:2]) for p in parent.Points)
            metrics = _ring_metrics(points, unit_mm)
            # A room may be drawn as an outer boundary plus inner islands;
            # the largest ring is the room.
            if metrics is not None and (best is None or metrics[0] > best[0]):
                best = metrics
        if best is not None:
            return best

    for item in _items_of(space, "Box"):
        if item.is_a("IfcBoundingBox"):
            x_dim, y_dim = getattr(item, "XDim", None), getattr(item, "YDim", None)
            if x_dim and y_dim:
                dx, dy = float(x_dim) * unit_mm, float(y_dim) * unit_mm
                return (dx / 1000.0) * (dy / 1000.0), min(dx, dy)
    return None


def _quantity_area_m2(space, area_to_m2: float) -> float | None:
    """A floor-area quantity on the space, converted to m2."""
    best = None
    for rel in getattr(space, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        quantity_set = getattr(rel, "RelatingPropertyDefinition", None)
        if quantity_set is None or not quantity_set.is_a("IfcElementQuantity"):
            continue
        for item in getattr(quantity_set, "Quantities", None) or []:
            if not item.is_a("IfcQuantityArea"):
                continue
            name = str(getattr(item, "Name", ""))
            value = getattr(item, "AreaValue", None)
            if not value:
                continue
            area_m2 = float(value) * area_to_m2
            if name in ("NetFloorArea", "GrossFloorArea"):
                return area_m2
            if best is None:
                best = area_m2
    return best


def check_rule(model: ifcopenshell.file) -> dict:
    spaces = model.by_type("IfcSpace")

    if not spaces:
        return {
            "verdict": "not_applicable",
            "violations": [],
            "violation_count": 0,
            "unknown_reasons": [],
            "checked_summary": {},
            "summary": "No IfcSpace elements found in the model.",
            "checks": [],
        }

    unit_mm = length_unit_to_mm(model)
    area_to_m2 = _area_unit_to_m2(model, unit_mm)

    violations = []
    checks = []
    checked = {COND_AREA: 0, COND_WIDTH: 0}
    skipped = {COND_AREA: 0, COND_WIDTH: 0}
    unclassified = 0
    no_area = 0
    no_width = 0
    out_of_scope = 0

    for space in spaces:
        kind = _classify(space)
        if kind is None:
            unclassified += 1
            skipped[COND_AREA] += 1
            skipped[COND_WIDTH] += 1
            continue
        if kind == "out_of_scope":
            out_of_scope += 1
            continue

        habitable = kind == "habitable"
        required_area = MIN_HABITABLE_AREA_M2 if habitable else MIN_OTHER_AREA_M2
        required_width = MIN_HABITABLE_WIDTH_MM if habitable else MIN_OTHER_WIDTH_MM

        footprint = _footprint(space, unit_mm)
        area_m2 = _quantity_area_m2(space, area_to_m2)
        if area_m2 is None or not (MIN_PLAUSIBLE_AREA_M2 <= area_m2 <= MAX_PLAUSIBLE_AREA_M2):
            # A missing or implausible quantity is replaced by the geometry,
            # which is the measurement of record either way.
            area_m2 = footprint[0] if footprint else None
        width_mm = footprint[1] if footprint else None
        # Unit conversion leaves float noise; round to a tenth of a
        # millimetre / square centimetre so an exactly-compliant room is not
        # reported as a violation.
        if area_m2 is not None:
            area_m2 = round(area_m2, 4)
        if width_mm is not None:
            width_mm = round(width_mm, 1)

        label = element_label(space)
        storey = element_storey(space)

        if area_m2 is None:
            no_area += 1
            skipped[COND_AREA] += 1
        elif not (MIN_PLAUSIBLE_AREA_M2 <= area_m2 <= MAX_PLAUSIBLE_AREA_M2):
            out_of_scope += 1
            skipped[COND_AREA] += 1
            skipped[COND_WIDTH] += 1
            continue
        else:
            checked[COND_AREA] += 1
            area_measured = (
                f"area={area_m2:.2f} m2, required={required_area:.1f} m2, "
                f"room_type={kind}"
            )
            area_threshold = f">= {required_area:.1f} m2"
            area_is_violation = area_m2 < required_area
            checks.append({
                "element": label,
                "storey": storey,
                "criterion": "Room area",
                "measured": area_measured,
                "threshold": area_threshold,
                "result": "fail" if area_is_violation else "pass",
            })
            if area_is_violation:
                violations.append({
                    "condition": COND_AREA,
                    "description": "Room net floor area is below the minimum required.",
                    "rule_ref": RULE_REF,
                    "threshold": area_threshold,
                    "locations": [{
                        "element": label,
                        "storey": storey,
                        "measured": area_measured,
                    }],
                })

        if width_mm is None:
            no_width += 1
            skipped[COND_WIDTH] += 1
        else:
            checked[COND_WIDTH] += 1
            width_measured = (
                f"width={width_mm:.0f} mm, required={required_width:.0f} mm, "
                f"room_type={kind}"
            )
            width_threshold = f">= {required_width:.0f} mm"
            width_is_violation = width_mm < required_width
            checks.append({
                "element": label,
                "storey": storey,
                "criterion": "Least width",
                "measured": width_measured,
                "threshold": width_threshold,
                "result": "fail" if width_is_violation else "pass",
            })
            if width_is_violation:
                violations.append({
                    "condition": COND_WIDTH,
                    "description": "Room least lateral dimension is below the minimum required.",
                    "rule_ref": RULE_REF,
                    "threshold": width_threshold,
                    "locations": [{
                        "element": label,
                        "storey": storey,
                        "measured": width_measured,
                    }],
                })

    unknown_reasons = []
    if unclassified:
        unknown_reasons.append({
            "condition": COND_AREA,
            "missing": "Name/LongName identifying the space as a habitable or other room",
            "affected_elements": unclassified,
        })
    if no_area:
        unknown_reasons.append({
            "condition": COND_AREA,
            "missing": "floor-area quantity or usable body geometry on IfcSpace",
            "affected_elements": no_area,
        })
    if no_width:
        unknown_reasons.append({
            "condition": COND_WIDTH,
            "missing": "usable plan footprint geometry on IfcSpace",
            "affected_elements": no_width,
        })

    checked_summary = {
        COND_AREA: {
            "elements_checked": checked[COND_AREA],
            "elements_skipped": skipped[COND_AREA],
            "skip_reasons": {
                k: v for k, v in (
                    ("unclassified_space", unclassified),
                    ("missing_area", no_area),
                    ("out_of_scope_space", out_of_scope),
                ) if v
            },
        },
        COND_WIDTH: {
            "elements_checked": checked[COND_WIDTH],
            "elements_skipped": skipped[COND_WIDTH],
            "skip_reasons": {
                k: v for k, v in (
                    ("unclassified_space", unclassified),
                    ("missing_footprint", no_width),
                ) if v
            },
        },
    }

    total_checked = checked[COND_AREA] + checked[COND_WIDTH]
    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} room dimension(s) fall below the BNBC minimum "
            f"({MIN_HABITABLE_AREA_M2:.1f} m2 / {MIN_HABITABLE_WIDTH_MM:.0f} mm for habitable rooms)."
        )
    elif total_checked == 0:
        if unclassified or no_area or no_width:
            verdict = "unknown"
            summary = (
                "No space has both a usable size and a recognisable room type; "
                "compliance cannot be determined."
            )
        else:
            verdict = "not_applicable"
            summary = "No room governed by Sec 1.14.2.2 found in the model."
    else:
        verdict = "pass"
        summary = (
            f"All checked room(s) meet the minimum area and width "
            f"({checked[COND_AREA]} area check(s), {checked[COND_WIDTH]} width check(s))."
        )

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
