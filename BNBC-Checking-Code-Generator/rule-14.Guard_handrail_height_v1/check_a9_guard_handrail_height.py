"""
Rule A9 — Guard and handrail height
(BNBC 2020 Part 3 Sec 1.14.14 and Sec 1.14.5.6).

Requirement:
  guard / parapet at an accessible flat roof or open edge   >= 1000 mm
  stair handrail, measured from the nose of the stair       >=  900 mm

Sec 1.14.14: "All accessible flat roofs shall be enclosed by parapets or
guardrails having a height of at least 1 m."
Sec 1.14.5.6: "Handrails shall have a minimum height of 0.9 m measured from
the nose of stair to the top of the handrail."

Conventions adopted:

* Which limit applies. IFC2X3 leaves IfcRailing.PredefinedType NOTDEFINED in
  every model of this corpus, so guard vs handrail is decided from the
  element name (English and Dutch, e.g. "Guard Rail", "doorvalregel",
  "traphek" -> guard; "Handrail", "leuning" -> handrail). A railing that
  matches neither is reported unknown rather than assigned the lenient
  limit — assuming "handrail" on an unlabelled railing would turn a
  1 m guard requirement into a 0.9 m one and hide real violations.

* Measurement. Pset_RailingCommon.Height is the authored height and is used
  first. Where it is absent the vertical extent of the railing's own body
  geometry is used, which includes any base offset and so reads slightly
  high on a railing modelled from the floor rather than from the nosing.

* Only IfcRailing is examined. A parapet built as a wall rather than a
  railing satisfies Sec 1.14.14 through Sec 7.4.9.4's separate thickness and
  height limits, which no rule in this set currently checks, so such a
  parapet is neither passed nor failed here.

Usage:
    python check_a9_guard_handrail_height.py <path-to-ifc>
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

RULE_ID = "A9"
RULE_REF = "BNBC 2020 Part 3 Sec 1.14.14; Sec 1.14.5.6"
MIN_GUARD_HEIGHT_MM = 1000.0
MIN_HANDRAIL_HEIGHT_MM = 900.0
COND_GUARD = "guard_min_height"
COND_HANDRAIL = "stair_handrail_min_height"

# A "railing" measuring less than this is a kerb, a wall-mounted grab bar or
# a modelling artefact, not a guard or a handrail anyone leans on.
MIN_PLAUSIBLE_HEIGHT_MM = 300.0
MAX_PLAUSIBLE_HEIGHT_MM = 3000.0

_GUARD_KEYWORDS = (
    "guard", "balustrade", "parapet", "barrier", "doorvalregel", "traphek",
    "afscheiding", "hekwerk", "borstwering",
)
_HANDRAIL_KEYWORDS = ("handrail", "hand rail", "leuning", "trapleuning")


def _classify(railing) -> str | None:
    """'guard' | 'handrail' | None (undecidable)."""
    predefined = str(getattr(railing, "PredefinedType", None) or "").upper()
    if predefined in ("GUARDRAIL", "BALUSTRADE"):
        return "guard"
    if predefined == "HANDRAIL":
        return "handrail"

    text = " ".join(
        str(getattr(railing, attr, None) or "") for attr in ("Name", "ObjectType", "Description")
    ).lower()
    if not text.strip():
        return None
    # Guard first: "900mm Pipe Guard Rail" must not be read as a handrail
    # merely because it is a rail.
    if any(k in text for k in _GUARD_KEYWORDS):
        return "guard"
    if any(k in text for k in _HANDRAIL_KEYWORDS):
        return "handrail"
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


def _geometry_height_mm(railing, unit_mm: float) -> float | None:
    """Vertical extent of the railing's own geometry, in mm."""
    for item in _items_of(railing, "Box"):
        if item.is_a("IfcBoundingBox") and getattr(item, "ZDim", None):
            return float(item.ZDim) * unit_mm

    zs: list[float] = []
    for item in _items_of(railing, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            direction = getattr(getattr(item, "ExtrudedDirection", None), "DirectionRatios", None)
            depth = getattr(item, "Depth", None)
            if depth and direction is not None and abs(direction[2]) > 0.9:
                return float(depth) * unit_mm
        if item.is_a("IfcFacetedBrep"):
            shell = getattr(item, "Outer", None)
            for face in getattr(shell, "CfsFaces", None) or []:
                for bound in getattr(face, "Bounds", None) or []:
                    loop = getattr(bound, "Bound", None)
                    if loop is not None and loop.is_a("IfcPolyLoop"):
                        zs.extend(p.Coordinates[2] for p in loop.Polygon)
    if zs:
        return (max(zs) - min(zs)) * unit_mm
    return None


def _height_mm(railing, unit_mm: float) -> float | None:
    psets = property_sets(railing)
    for pset_name in ("Pset_RailingCommon", "Pset_Railing"):
        value = psets.get(pset_name, {}).get("Height")
        if value:
            return float(value) * unit_mm
    return _geometry_height_mm(railing, unit_mm)


def check_rule(model: ifcopenshell.file) -> dict:
    railings = model.by_type("IfcRailing")

    if not railings:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcRailing elements found in the model.",
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checked = {COND_GUARD: 0, COND_HANDRAIL: 0}
    skipped = {COND_GUARD: 0, COND_HANDRAIL: 0}
    unclassified = 0
    no_height = 0
    implausible = 0

    for railing in railings:
        kind = _classify(railing)
        if kind is None:
            unclassified += 1
            skipped[COND_GUARD] += 1
            continue

        condition = COND_GUARD if kind == "guard" else COND_HANDRAIL
        required = MIN_GUARD_HEIGHT_MM if kind == "guard" else MIN_HANDRAIL_HEIGHT_MM

        height_mm = _height_mm(railing, unit_mm)
        if height_mm is not None:
            # Unit conversion leaves float noise (0.9 m -> 899.9999999999999),
            # which would report an exactly-compliant railing as a violation.
            # A tenth of a millimetre is already far below what any of this
            # is measured to.
            height_mm = round(height_mm, 1)
        if height_mm is None:
            no_height += 1
            skipped[condition] += 1
            continue
        if not (MIN_PLAUSIBLE_HEIGHT_MM <= height_mm <= MAX_PLAUSIBLE_HEIGHT_MM):
            implausible += 1
            skipped[condition] += 1
            continue

        checked[condition] += 1
        if height_mm < required:
            violations.append({
                "condition": condition,
                "description": (
                    "Guard or parapet height is below the minimum required."
                    if kind == "guard"
                    else "Stair handrail height is below the minimum required."
                ),
                "rule_ref": RULE_REF,
                "threshold": f">= {required:.0f} mm",
                "locations": [{
                    "element": element_label(railing),
                    "storey": element_storey(railing),
                    "measured": (
                        f"height={height_mm:.0f} mm, required={required:.0f} mm, "
                        f"railing_type={kind}"
                    ),
                }],
            })

    unknown_reasons = []
    if unclassified:
        unknown_reasons.append({
            "condition": COND_GUARD,
            "missing": "PredefinedType or Name identifying the railing as a guard or a handrail",
            "affected_elements": unclassified,
        })
    if no_height:
        unknown_reasons.append({
            "condition": COND_GUARD,
            "missing": "Pset_RailingCommon.Height or usable body geometry on IfcRailing",
            "affected_elements": no_height,
        })

    checked_summary = {
        COND_GUARD: {
            "elements_checked": checked[COND_GUARD],
            "elements_skipped": skipped[COND_GUARD],
            "skip_reasons": {
                k: v for k, v in (
                    ("unclassified_railing", unclassified),
                    ("missing_height", no_height),
                    ("implausible_height", implausible),
                ) if v
            },
        },
        COND_HANDRAIL: {
            "elements_checked": checked[COND_HANDRAIL],
            "elements_skipped": skipped[COND_HANDRAIL],
            "skip_reasons": {"missing_height": skipped[COND_HANDRAIL]} if skipped[COND_HANDRAIL] else {},
        },
    }

    total_checked = checked[COND_GUARD] + checked[COND_HANDRAIL]
    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} railing(s) are below the required height "
            f"({MIN_GUARD_HEIGHT_MM:.0f} mm guard / {MIN_HANDRAIL_HEIGHT_MM:.0f} mm handrail)."
        )
    elif total_checked == 0:
        verdict = "unknown"
        summary = (
            f"None of the {len(railings)} railing(s) have both a usable height and a "
            f"recognisable type; compliance cannot be determined."
        )
    else:
        verdict = "pass"
        summary = (
            f"All {total_checked} checked railing(s) meet the required height "
            f"({checked[COND_GUARD]} guard(s), {checked[COND_HANDRAIL]} handrail(s))."
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
