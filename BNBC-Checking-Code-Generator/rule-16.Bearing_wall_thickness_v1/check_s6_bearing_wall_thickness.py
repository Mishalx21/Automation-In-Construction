"""
Rule S6 — Minimum thickness of load-bearing walls
(BNBC 2020 Part 6 Sec 7.4.9.1 for masonry; Sec 6.6.5.3.1 for concrete).

Requirement:
  masonry bearing wall      nominal thickness >= 250 mm
                            (Sec 7.4.9.1; the stiffened single-storey
                            exception of 165 mm is noted below)
  concrete bearing wall     thickness >= 1/25 of the supported height,
                            and never less than 100 mm (Sec 6.6.5.3.1)

Conventions adopted:

* Scope. Only walls flagged Pset_WallCommon.LoadBearing = True are checked;
  a partition carries no vertical load and neither clause applies to it.
  Where no wall in the model carries that flag at all, the rule reports
  not_applicable rather than treating every partition as structural.

* Which limit applies. The wall's material decides it. Layer material names
  are matched against masonry terms (brick, block, masonry, and the Dutch
  metselwerk / baksteen / kalkzandsteen this corpus uses) and concrete terms
  (concrete, beton). A load-bearing wall whose material cannot be identified
  is reported unknown — applying the 250 mm masonry limit to an unidentified
  wall would condemn compliant reinforced-concrete shear walls, and applying
  the concrete limit would excuse thin masonry.

* Thickness. Taken from the total of the wall's IfcMaterialLayerSet, which
  is the nominal thickness the clause names. Where no layer set exists, the
  smallest dimension of the wall's own body geometry is used instead.

* Supported height. Needed only for the concrete limit. Read from the
  vertical extrusion depth of the wall body, falling back to the distance to
  the next storey above. A concrete wall with no resolvable height is
  checked against the absolute 100 mm floor alone, and that is stated in the
  measurement.

* The Sec 7.4.9.1 exception (165 mm for a stiffened solid masonry bearing
  wall in a one-storey building not over 3 m high) is not applied
  automatically: "stiffened" is a design property IFC does not carry. A
  model that relies on it will show a reportable violation here, which is
  the safe direction for a screening check.

Usage:
    python check_s6_bearing_wall_thickness.py <path-to-ifc>
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

RULE_ID = "S6"
RULE_REF = "BNBC 2020 Part 6 Sec 7.4.9.1 (masonry); Sec 6.6.5.3.1 (concrete)"
MIN_MASONRY_THICKNESS_MM = 250.0
MIN_CONCRETE_THICKNESS_MM = 100.0
CONCRETE_HEIGHT_RATIO = 25.0
COND_MASONRY = "masonry_bearing_wall_min_thickness"
COND_CONCRETE = "concrete_bearing_wall_min_thickness"

# Outside this band the "wall" is a skirting, a joint filler or a whole
# facade assembly swept into one solid, not a structural wall leaf.
MIN_PLAUSIBLE_THICKNESS_MM = 40.0
MAX_PLAUSIBLE_THICKNESS_MM = 2000.0

_MASONRY_KEYWORDS = (
    "masonry", "brick", "block", "cmu", "clay", "metselwerk", "baksteen",
    "kalkzandsteen", "mw-", "mauerwerk",
)
_CONCRETE_KEYWORDS = ("concrete", "beton", "reinforced")


def _layer_thickness_mm(wall, unit_mm: float):
    """(total thickness mm, [material names]) from the wall's layer set."""
    for rel in getattr(wall, "HasAssociations", None) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = getattr(rel, "RelatingMaterial", None)
        layer_set = None
        if material is None:
            continue
        if material.is_a("IfcMaterialLayerSetUsage"):
            layer_set = getattr(material, "ForLayerSet", None)
        elif material.is_a("IfcMaterialLayerSet"):
            layer_set = material
        if layer_set is None:
            continue
        layers = getattr(layer_set, "MaterialLayers", None) or []
        if not layers:
            continue
        total = sum(float(getattr(layer, "LayerThickness", 0.0) or 0.0) for layer in layers)
        names = [
            str(getattr(getattr(layer, "Material", None), "Name", "") or "")
            for layer in layers
        ]
        if total > 0:
            return total * unit_mm, names
    return None, []


def _material_names(wall) -> list[str]:
    """Every material name attached to the wall, however it is attached."""
    names: list[str] = []
    for rel in getattr(wall, "HasAssociations", None) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = getattr(rel, "RelatingMaterial", None)
        if material is None:
            continue
        if material.is_a("IfcMaterial"):
            names.append(str(getattr(material, "Name", "") or ""))
        elif material.is_a("IfcMaterialList"):
            names.extend(str(getattr(m, "Name", "") or "") for m in material.Materials)
        elif material.is_a("IfcMaterialLayerSetUsage") or material.is_a("IfcMaterialLayerSet"):
            layer_set = material if material.is_a("IfcMaterialLayerSet") else material.ForLayerSet
            for layer in getattr(layer_set, "MaterialLayers", None) or []:
                names.append(str(getattr(getattr(layer, "Material", None), "Name", "") or ""))
    return [n for n in names if n]


def _classify_material(names) -> str | None:
    """'masonry' | 'concrete' | None (undecidable)."""
    text = " ".join(names).lower()
    if not text.strip():
        return None
    if any(k in text for k in _MASONRY_KEYWORDS):
        return "masonry"
    if any(k in text for k in _CONCRETE_KEYWORDS):
        return "concrete"
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


def _profile_extents(profile):
    if profile is None:
        return None
    if profile.is_a("IfcRectangleProfileDef"):
        return float(profile.XDim), float(profile.YDim)
    curve = getattr(profile, "OuterCurve", None)
    points = []
    if curve is not None and curve.is_a("IfcPolyline"):
        points = [tuple(p.Coordinates[:2]) for p in curve.Points]
    elif curve is not None and curve.is_a("IfcCompositeCurve"):
        for segment in getattr(curve, "Segments", None) or []:
            parent = getattr(segment, "ParentCurve", None)
            if parent is not None and parent.is_a("IfcPolyline"):
                points.extend(tuple(p.Coordinates[:2]) for p in parent.Points)
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(xs) - min(xs), max(ys) - min(ys)


def _geometry_thickness_and_height(wall, unit_mm: float):
    """(thickness mm, height mm) from the wall's own body, either may be None."""
    for item in _items_of(wall, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            extents = _profile_extents(getattr(item, "SweptArea", None))
            depth = getattr(item, "Depth", None)
            direction = getattr(getattr(item, "ExtrudedDirection", None), "DirectionRatios", None)
            vertical = direction is None or abs(direction[2]) > 0.9
            # A wall is extruded upward from its plan footprint: the
            # footprint's short side is the thickness, the depth the height.
            thickness = min(extents) * unit_mm if extents else None
            height = float(depth) * unit_mm if (depth and vertical) else None
            if thickness or height:
                return thickness, height
        if item.is_a("IfcFacetedBrep"):
            xs, ys, zs = [], [], []
            shell = getattr(item, "Outer", None)
            for face in getattr(shell, "CfsFaces", None) or []:
                for bound in getattr(face, "Bounds", None) or []:
                    loop = getattr(bound, "Bound", None)
                    if loop is not None and loop.is_a("IfcPolyLoop"):
                        for point in loop.Polygon:
                            xs.append(point.Coordinates[0])
                            ys.append(point.Coordinates[1])
                            zs.append(point.Coordinates[2])
            if xs:
                plan = [(max(xs) - min(xs)) * unit_mm, (max(ys) - min(ys)) * unit_mm]
                return min(plan), (max(zs) - min(zs)) * unit_mm
    return None, None


def _storey_height_mm(model, wall, unit_mm: float) -> float | None:
    """Distance from the wall's storey to the next storey above, in mm."""
    storeys = [s for s in model.by_type("IfcBuildingStorey") if s.Elevation is not None]
    if len(storeys) < 2:
        return None
    own = None
    for rel in model.get_inverse(wall):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            structure = getattr(rel, "RelatingStructure", None)
            if structure is not None and structure.is_a("IfcBuildingStorey"):
                own = structure
                break
    if own is None or own.Elevation is None:
        return None
    above = [s.Elevation for s in storeys if s.Elevation > own.Elevation]
    if not above:
        return None
    return (min(above) - own.Elevation) * unit_mm


def _is_load_bearing(wall) -> bool | None:
    value = property_sets(wall).get("Pset_WallCommon", {}).get("LoadBearing")
    return None if value is None else bool(value)


def check_rule(model: ifcopenshell.file) -> dict:
    walls = model.by_type("IfcWall")

    if not walls:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcWall elements found in the model.",
            "checks": [],
        }

    bearing = [w for w in walls if _is_load_bearing(w) is True]
    if not bearing:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": (
                f"None of the {len(walls)} wall(s) are flagged "
                f"Pset_WallCommon.LoadBearing; no bearing wall to check."
            ),
            "checks": [],
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checks = []
    checked = {COND_MASONRY: 0, COND_CONCRETE: 0}
    unknown_material = 0
    no_thickness = 0
    implausible = 0
    no_height = 0

    for wall in bearing:
        thickness_mm, layer_names = _layer_thickness_mm(wall, unit_mm)
        geometry_thickness, geometry_height = _geometry_thickness_and_height(wall, unit_mm)
        if thickness_mm is None:
            thickness_mm = geometry_thickness
        if thickness_mm is None:
            no_thickness += 1
            continue
        thickness_mm = round(thickness_mm, 1)
        if not (MIN_PLAUSIBLE_THICKNESS_MM <= thickness_mm <= MAX_PLAUSIBLE_THICKNESS_MM):
            implausible += 1
            continue

        kind = _classify_material(layer_names or _material_names(wall))
        if kind is None:
            unknown_material += 1
            continue

        label = element_label(wall)
        storey = element_storey(wall)

        if kind == "masonry":
            checked[COND_MASONRY] += 1
            measured = (
                f"thickness={thickness_mm:.0f} mm, "
                f"required={MIN_MASONRY_THICKNESS_MM:.0f} mm, material=masonry"
            )
            threshold = f">= {MIN_MASONRY_THICKNESS_MM:.0f} mm"
            is_violation = thickness_mm < MIN_MASONRY_THICKNESS_MM
            checks.append(
                {
                    "element": label,
                    "storey": storey,
                    "measured": measured,
                    "threshold": threshold,
                    "result": "fail" if is_violation else "pass",
                }
            )
            if is_violation:
                violations.append({
                    "condition": COND_MASONRY,
                    "description": "Masonry bearing wall is thinner than the nominal minimum.",
                    "rule_ref": RULE_REF,
                    "threshold": threshold,
                    "locations": [{
                        "element": label, "storey": storey,
                        "measured": measured,
                    }],
                })
            continue

        height_mm = geometry_height or _storey_height_mm(model, wall, unit_mm)
        if height_mm is None:
            no_height += 1
            required = MIN_CONCRETE_THICKNESS_MM
            basis = "no resolvable height, checked against the 100 mm floor only"
        else:
            height_mm = round(height_mm, 1)
            required = max(MIN_CONCRETE_THICKNESS_MM, height_mm / CONCRETE_HEIGHT_RATIO)
            basis = f"height={height_mm:.0f} mm, h/25={height_mm / CONCRETE_HEIGHT_RATIO:.0f} mm"

        checked[COND_CONCRETE] += 1
        measured = (
            f"thickness={thickness_mm:.0f} mm, required={required:.0f} mm, "
            f"material=concrete, {basis}"
        )
        threshold = f">= {required:.0f} mm"
        is_violation = thickness_mm < required
        checks.append(
            {
                "element": label,
                "storey": storey,
                "measured": measured,
                "threshold": threshold,
                "result": "fail" if is_violation else "pass",
            }
        )
        if is_violation:
            violations.append({
                "condition": COND_CONCRETE,
                "description": (
                    "Concrete bearing wall is thinner than 1/25 of its supported "
                    "height or than the 100 mm absolute minimum."
                ),
                "rule_ref": RULE_REF,
                "threshold": threshold,
                "locations": [{
                    "element": label, "storey": storey,
                    "measured": measured,
                }],
            })

    unknown_reasons = []
    if unknown_material:
        unknown_reasons.append({
            "condition": COND_MASONRY,
            "missing": "material name identifying the bearing wall as masonry or concrete",
            "affected_elements": unknown_material,
        })
    if no_thickness:
        unknown_reasons.append({
            "condition": COND_MASONRY,
            "missing": "IfcMaterialLayerSet or usable body geometry on the bearing wall",
            "affected_elements": no_thickness,
        })
    if no_height:
        unknown_reasons.append({
            "condition": COND_CONCRETE,
            "missing": "supported height (extrusion depth or storey spacing)",
            "affected_elements": no_height,
        })

    skip_reasons = {
        k: v for k, v in (
            ("unidentified_material", unknown_material),
            ("missing_thickness", no_thickness),
            ("implausible_thickness", implausible),
        ) if v
    }
    total_checked = checked[COND_MASONRY] + checked[COND_CONCRETE]
    checked_summary = {
        COND_MASONRY: {
            "elements_checked": checked[COND_MASONRY],
            "elements_skipped": len(bearing) - total_checked,
            "skip_reasons": skip_reasons,
        },
        COND_CONCRETE: {
            "elements_checked": checked[COND_CONCRETE],
            "elements_skipped": len(bearing) - total_checked,
            "skip_reasons": skip_reasons,
        },
    }

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} of {total_checked} checked bearing wall(s) are below the "
            f"minimum thickness."
        )
    elif total_checked == 0:
        verdict = "unknown"
        summary = (
            f"None of the {len(bearing)} bearing wall(s) have both a usable thickness "
            f"and an identifiable material; compliance cannot be determined."
        )
    else:
        verdict = "pass"
        summary = (
            f"All {total_checked} checked bearing wall(s) meet the minimum thickness "
            f"({checked[COND_MASONRY]} masonry, {checked[COND_CONCRETE]} concrete)."
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
