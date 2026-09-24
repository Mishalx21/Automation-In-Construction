"""
Rule A6 — Ceiling height of rooms and egress corridors
(BNBC 2020 Part 3 Sec 1.14.2.1(a); Part 4 Chapter 3 Sec 3.7.3).

Requirement:
  habitable room      clear ceiling height >= 2750 mm
  egress corridor     clear ceiling height >= 2400 mm

Conventions adopted:

* Occupancy. BNBC Table 3.1.9 raises the minimum to 3.0 m (educational,
  institutional, health care, assembly) and 3.5 m (industrial, storage,
  hazardous), and allows 2.44 m for air-conditioned rooms. IFC carries no
  BNBC occupancy classification, so this checker applies only the general
  2.75 m habitable minimum of Sec 1.14.2.1(a). On an educational or health
  care model the result is therefore permissive, never falsely strict.

* Measurement. "Clear ceiling height" is the floor surface to the underside
  of the finished ceiling. IFC rarely carries that number directly, so the
  height is read in this order: a Height / NetHeight / GrossHeight quantity
  on the space, then the vertical extent of the space's own body geometry.
  Authoring tools set the space solid from the room's upper limit, which
  equals the clear height only where the space was modelled to the ceiling
  rather than to the slab above — this is a genuine interpretation gap, and
  on a model where spaces run slab-to-slab the measured value overstates the
  clear height. Spaces with neither source are reported unknown, never
  passed.

* Classification. Habitable / non-habitable / corridor is decided from the
  space's LongName and Name (English and Dutch keywords, matching the corpus).
  A space that matches nothing is skipped and counted as unknown rather than
  assumed habitable.

Usage:
    python check_a6_ceiling_height.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, length_unit_to_mm  # noqa: E402

RULE_ID = "A6"
RULE_REF = "BNBC 2020 Part 3 Sec 1.14.2.1(a); Part 4 Ch.3 Sec 3.7.3"
MIN_HABITABLE_HEIGHT_MM = 2750.0
MIN_CORRIDOR_HEIGHT_MM = 2400.0
COND_ROOM = "room_min_ceiling_height"
COND_CORRIDOR = "corridor_min_ceiling_height"

# A modelled height below this is a shaft stub, a void or a placeholder
# rather than an occupiable room, and flagging it would be noise.
MIN_PLAUSIBLE_HEIGHT_MM = 1200.0

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
_NON_HABITABLE_KEYWORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower",
    "store", "storage", "stor", "stair", "mechanical", "mech", "electrical",
    "elec", "janitor", "jan.", "jan ", "closet", "shaft", "riser", "chase",
    "elevator", "elev", "lift", "garage", "parking", "roof", "utility",
    "utl", "plant", "duct", "kitchen", "pantry", "laundry", "void",
    "open to below", "trash", "recycl", "server", "tele", "comm.", "comm rm",
    "equip", "instal", "berging", "trap", "badkamer", "keuken", "meterkast",
    "kast", "onben",
)


def _space_text(space) -> str:
    return " ".join(
        str(getattr(space, attr, None) or "") for attr in ("LongName", "Name")
    ).lower()


def _classify(space) -> str | None:
    """'corridor' | 'habitable' | 'non_habitable' | None (undecidable)."""
    text = _space_text(space)
    if not text.strip():
        return None
    if any(k in text for k in _CORRIDOR_KEYWORDS):
        return "corridor"
    if any(k in text for k in _NON_HABITABLE_KEYWORDS):
        return "non_habitable"
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return "habitable"
    return None


def _quantity_height(space, unit_mm: float) -> float | None:
    """A Height / NetHeight / GrossHeight length quantity, in mm."""
    for rel in getattr(space, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        quantity_set = getattr(rel, "RelatingPropertyDefinition", None)
        if quantity_set is None or not quantity_set.is_a("IfcElementQuantity"):
            continue
        for item in getattr(quantity_set, "Quantities", None) or []:
            if not item.is_a("IfcQuantityLength"):
                continue
            if str(getattr(item, "Name", "")) in ("Height", "NetHeight", "GrossHeight"):
                value = getattr(item, "LengthValue", None)
                if value:
                    return float(value) * unit_mm
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


def _geometry_height(space, unit_mm: float) -> float | None:
    """Vertical extent of the space's own geometry, in mm.

    Parses the representation directly instead of tessellating: a space is
    an extrusion, a Brep or (in ArchiCAD exports) nothing more than a
    bounding box, and all three carry the height without meshing.
    """
    for item in _items_of(space, "Box"):
        if item.is_a("IfcBoundingBox"):
            z_dim = getattr(item, "ZDim", None)
            if z_dim:
                return float(z_dim) * unit_mm

    for item in _items_of(space, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            depth = getattr(item, "Depth", None)
            direction = getattr(getattr(item, "ExtrudedDirection", None), "DirectionRatios", None)
            # Only a vertical extrusion's Depth is the room height.
            if depth and (direction is None or abs(direction[2]) > 0.9):
                return float(depth) * unit_mm
        if item.is_a("IfcFacetedBrep"):
            zs = []
            shell = getattr(item, "Outer", None)
            for face in getattr(shell, "CfsFaces", None) or []:
                for bound in getattr(face, "Bounds", None) or []:
                    loop = getattr(bound, "Bound", None)
                    if loop is not None and loop.is_a("IfcPolyLoop"):
                        zs.extend(p.Coordinates[2] for p in loop.Polygon)
            if zs:
                return (max(zs) - min(zs)) * unit_mm
    return None


def _space_height_mm(space, unit_mm: float) -> float | None:
    return _quantity_height(space, unit_mm) or _geometry_height(space, unit_mm)


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
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checked = {COND_ROOM: 0, COND_CORRIDOR: 0}
    skipped = {COND_ROOM: 0, COND_CORRIDOR: 0}
    no_height = 0
    unclassified = 0
    out_of_scope = 0

    for space in spaces:
        kind = _classify(space)
        if kind is None:
            unclassified += 1
            skipped[COND_ROOM] += 1
            continue
        if kind == "non_habitable":
            out_of_scope += 1
            continue

        condition = COND_CORRIDOR if kind == "corridor" else COND_ROOM
        required = (
            MIN_CORRIDOR_HEIGHT_MM if kind == "corridor" else MIN_HABITABLE_HEIGHT_MM
        )

        height_mm = _space_height_mm(space, unit_mm)
        if height_mm is not None:
            # Unit conversion leaves float noise (2.75 m -> 2749.9999999999995),
            # which would report an exactly-compliant room as a violation.
            height_mm = round(height_mm, 1)
        if height_mm is None:
            no_height += 1
            skipped[condition] += 1
            continue
        if height_mm < MIN_PLAUSIBLE_HEIGHT_MM:
            out_of_scope += 1
            continue

        checked[condition] += 1
        if height_mm < required:
            violations.append({
                "condition": condition,
                "description": (
                    "Egress corridor clear height is below the minimum required."
                    if kind == "corridor"
                    else "Habitable room clear ceiling height is below the minimum required."
                ),
                "rule_ref": RULE_REF,
                "threshold": f">= {required:.0f} mm",
                "locations": [{
                    "element": element_label(space),
                    "storey": element_storey(space),
                    "measured": (
                        f"height={height_mm:.0f} mm, required={required:.0f} mm, "
                        f"space_type={kind}"
                    ),
                }],
            })

    unknown_reasons = []
    if no_height:
        unknown_reasons.append({
            "condition": COND_ROOM,
            "missing": "Height quantity or usable body geometry on IfcSpace",
            "affected_elements": no_height,
        })
    if unclassified:
        unknown_reasons.append({
            "condition": COND_ROOM,
            "missing": "Name/LongName identifying the space as habitable or a corridor",
            "affected_elements": unclassified,
        })

    checked_summary = {}
    for condition in (COND_ROOM, COND_CORRIDOR):
        reasons = {}
        if condition == COND_ROOM:
            if no_height:
                reasons["missing_height"] = no_height
            if unclassified:
                reasons["unclassified_space"] = unclassified
            if out_of_scope:
                reasons["non_habitable_or_implausible"] = out_of_scope
        elif skipped[condition]:
            reasons["missing_height"] = skipped[condition]
        checked_summary[condition] = {
            "elements_checked": checked[condition],
            "elements_skipped": skipped[condition],
            "skip_reasons": reasons,
        }

    total_checked = checked[COND_ROOM] + checked[COND_CORRIDOR]
    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} space(s) fall below the required ceiling height "
            f"({MIN_HABITABLE_HEIGHT_MM:.0f} mm habitable / {MIN_CORRIDOR_HEIGHT_MM:.0f} mm corridor)."
        )
    elif total_checked == 0:
        if unclassified or no_height:
            verdict = "unknown"
            summary = (
                "No space has both a usable height and a recognisable occupancy type; "
                "compliance cannot be determined."
            )
        else:
            verdict = "not_applicable"
            summary = "No habitable room or egress corridor found in the model."
    else:
        verdict = "pass"
        summary = (
            f"All {total_checked} checked space(s) meet the required ceiling height "
            f"({checked[COND_ROOM]} room(s), {checked[COND_CORRIDOR]} corridor(s))."
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
