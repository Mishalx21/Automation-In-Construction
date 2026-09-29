"""
Rule S10 — Minimum footing thickness
(BNBC 2020 Part 6 Sec 6.8.7, with Sec 3.8.3 for light structures).

Requirement (Sec 6.8.7): "Depth of footing above bottom reinforcement shall
not be less than 150 mm for footings on soil, nor less than 300 mm for
footings on piles."

Sec 3.8.3 tabulates the same idea for light structures (two storeys or
fewer): 150 mm reinforced concrete on soil, 300 mm on piles, 200 mm plain
concrete, 250 mm masonry.

Conventions adopted:

* Measurement. The clause limits the depth ABOVE THE BOTTOM REINFORCEMENT,
  which is the overall thickness less the bottom cover. These models carry
  no rebar, so the overall thickness is measured instead — it is larger than
  the quantity the clause names by one cover depth (typically 50-75 mm), so
  the check can only miss a marginal violation, never invent one. A footing
  reported here is non-compliant by a margin wider than any cover.

* Thickness is the vertical extrusion depth of the footing's own body, the
  footing being extruded from its plan profile. A footing modelled as a mesh
  with no profile is reported unknown rather than measured from a bounding
  box.

* On soil or on piles. A footing is treated as pile-supported when its own
  name says so ("pile cap") or the model contains IfcPile elements, which
  raises its limit from 150 mm to 300 mm.

* Piles modelled as footings. Several exports classify the piles themselves
  as IfcFooting (one model in this corpus carries 444 "M_Pile-Steel Pipe"
  entries that way). A pile is not a footing and its length is not a
  thickness, so any element whose vertical extent exceeds its own plan
  dimensions — or whose name says pile without saying pile cap — is
  excluded from the check and counted separately.

* Sec 3.8.6's minimum depth of foundation below grade (1.5 m in cohesive
  soil, 2 m in cohesionless) is NOT checked: it is measured from existing
  ground level, and IFC carries no reliable grade datum.

Usage:
    python check_s10_footing_thickness.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, length_unit_to_mm  # noqa: E402

RULE_ID = "S10"
RULE_REF = "BNBC 2020 Part 6 Sec 6.8.7; Sec 3.8.3"
MIN_THICKNESS_ON_SOIL_MM = 150.0
MIN_THICKNESS_ON_PILES_MM = 300.0
COND_THICKNESS = "footing_min_thickness"

# Outside this band the element is a blinding layer, a levelling screed or a
# whole foundation raft swept into one solid, not a footing thickness.
MIN_PLAUSIBLE_THICKNESS_MM = 50.0
MAX_PLAUSIBLE_THICKNESS_MM = 5000.0

_PILE_CAP_KEYWORDS = ("pile cap", "pilecap", "poer", "paalkop")
_PILE_KEYWORDS = ("pile", "paal")


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
    """(short, long) in-plane extent of a profile, in native units."""
    if profile is None:
        return None
    if profile.is_a("IfcRectangleProfileDef"):
        dims = (float(profile.XDim), float(profile.YDim))
        return min(dims), max(dims)
    if profile.is_a("IfcCircleProfileDef") or profile.is_a("IfcCircleHollowProfileDef"):
        diameter = float(profile.Radius) * 2.0
        return diameter, diameter
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
    dims = (max(xs) - min(xs), max(ys) - min(ys))
    return min(dims), max(dims)


def _thickness_and_plan_mm(footing, unit_mm: float):
    """(thickness mm, shortest plan dimension mm) of a footing, or None."""
    for item in _items_of(footing, "Body"):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        direction = getattr(getattr(item, "ExtrudedDirection", None), "DirectionRatios", None)
        if direction is not None and abs(direction[2]) < 0.9:
            # Extruded sideways: this is a ground beam, not a pad footing
            # lying on its plan profile, and its Depth is a length.
            continue
        depth = getattr(item, "Depth", None)
        extents = _profile_extents(getattr(item, "SweptArea", None))
        if not depth or extents is None:
            continue
        return round(float(depth) * unit_mm, 1), round(extents[0] * unit_mm, 1)
    return None


def _support(footing, model_has_piles: bool) -> str | None:
    """'pile' | 'soil', or None when the element is itself a pile."""
    name = " ".join(
        str(getattr(footing, attr, None) or "") for attr in ("Name", "ObjectType")
    ).lower()
    if any(k in name for k in _PILE_CAP_KEYWORDS):
        return "pile"
    if any(k in name for k in _PILE_KEYWORDS):
        # "Pile" without "pile cap": the element is a pile classified as a
        # footing, not a footing sitting on piles.
        return None
    return "pile" if model_has_piles else "soil"


def check_rule(model: ifcopenshell.file) -> dict:
    footings = model.by_type("IfcFooting")

    if not footings:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {}, "checks": [],
            "summary": "No IfcFooting elements found in the model.",
        }

    unit_mm = length_unit_to_mm(model)
    model_has_piles = bool(model.by_type("IfcPile"))

    violations = []
    checks = []
    checked = 0
    piles_excluded = 0
    no_geometry = 0
    implausible = 0

    for footing in footings:
        support = _support(footing, model_has_piles)
        if support is None:
            piles_excluded += 1
            continue

        measured = _thickness_and_plan_mm(footing, unit_mm)
        if measured is None:
            no_geometry += 1
            continue
        thickness_mm, plan_mm = measured

        # A "footing" taller than it is wide is a pile, a pier or a pedestal.
        if plan_mm and thickness_mm > plan_mm:
            piles_excluded += 1
            continue
        if not (MIN_PLAUSIBLE_THICKNESS_MM <= thickness_mm <= MAX_PLAUSIBLE_THICKNESS_MM):
            implausible += 1
            continue

        required = (
            MIN_THICKNESS_ON_PILES_MM if support == "pile" else MIN_THICKNESS_ON_SOIL_MM
        )
        checked += 1
        measured = (
            f"thickness={thickness_mm:.0f} mm, required={required:.0f} mm, "
            f"support={support}, plan_min={plan_mm:.0f} mm"
        )
        threshold = f">= {required:.0f} mm"
        is_violation = thickness_mm < required
        checks.append({
            "element": element_label(footing),
            "storey": element_storey(footing),
            "measured": measured,
            "threshold": threshold,
            "result": "fail" if is_violation else "pass",
        })
        if is_violation:
            violations.append({
                "condition": COND_THICKNESS,
                "description": "Footing thickness is below the minimum required.",
                "rule_ref": RULE_REF,
                "threshold": threshold,
                "locations": [{
                    "element": element_label(footing),
                    "storey": element_storey(footing),
                    "measured": measured,
                }],
            })

    unknown_reasons = []
    if no_geometry:
        unknown_reasons.append({
            "condition": COND_THICKNESS,
            "missing": "vertically extruded body giving the footing's thickness",
            "affected_elements": no_geometry,
        })

    checked_summary = {COND_THICKNESS: {
        "elements_checked": checked,
        "elements_skipped": len(footings) - checked,
        "skip_reasons": {
            k: v for k, v in (
                ("pile_classified_as_footing", piles_excluded),
                ("missing_geometry", no_geometry),
                ("implausible_thickness", implausible),
            ) if v
        },
    }}

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} of {checked} checked footing(s) are thinner than the "
            f"minimum required."
        )
    elif checked == 0:
        verdict = "unknown" if no_geometry else "not_applicable"
        summary = (
            "No footing has a measurable thickness; compliance cannot be determined."
            if verdict == "unknown"
            else (
                f"All {len(footings)} IfcFooting element(s) are piles classified as "
                f"footings; none is a footing this clause governs."
            )
        )
    else:
        verdict = "pass"
        summary = (
            f"All {checked} checked footing(s) meet the minimum thickness "
            f"({MIN_THICKNESS_ON_SOIL_MM:.0f} mm on soil / "
            f"{MIN_THICKNESS_ON_PILES_MM:.0f} mm on piles)."
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
