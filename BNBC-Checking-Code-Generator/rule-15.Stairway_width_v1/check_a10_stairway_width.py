"""
Rule A10 — Minimum width of a stairway in the egress system
(BNBC 2020 Part 3 Sec 1.14.5.1, referring to Part 4 Table 4.3.6).

Requirement: the clear width of each stairway shall be at least the value
tabulated for the occupancy. The lowest value in Table 4.3.6 is 1120 mm
(residential A3-A5, and educational up to an occupant load of 130); a
hospital patient area or a school above 130 occupants requires 2235 mm.

Conventions adopted:

* Occupancy. IFC carries no BNBC occupancy classification and no occupant
  load, so the checker applies the lowest tabulated width, 1120 mm. On a
  hospital or a large school the real requirement is higher, so a pass here
  is a floor, not a clearance — the result is permissive, never falsely
  strict. This is the same occupancy gap A6 documents.

* Measurement. The width is the shorter horizontal side of the flight's
  bounding box; the longer one is the going. IfcStairFlight is the element
  the clause is about and is measured wherever the model has it.

* Fallback. Several exports model no IfcStairFlight at all and carry only a
  parent IfcStair. Those are measured instead, but an IfcStair spans the
  whole stair enclosure — two flights plus the well and the landings — so
  its short plan dimension OVERSTATES the width of any one flight. The
  fallback can therefore only miss a violation, never invent one, and each
  such measurement is reported with its basis so the number is not mistaken
  for a flight width.

* Nosings, stringers and handrail intrusion are not deducted: IFC does not
  model the clear width between handrails separately, so the measured value
  is the structural width of the flight.

Usage:
    python check_a10_stairway_width.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import (  # noqa: E402
    element_label, element_storey, geom_settings, get_bbox_mm,
)

RULE_ID = "A10"
RULE_REF = "BNBC 2020 Part 3 Sec 1.14.5.1; Part 4 Table 4.3.6"
MIN_STAIRWAY_WIDTH_MM = 1120.0
COND_WIDTH = "stairway_min_width"

# Outside this band the element is a ladder, a kerb detail or a whole
# multi-storey stair core swept into one solid, not a flight.
MIN_PLAUSIBLE_WIDTH_MM = 300.0
MAX_PLAUSIBLE_WIDTH_MM = 6000.0


def _plan_width_mm(element, settings) -> float | None:
    """Shorter horizontal side of the element's bounding box, in mm."""
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    bbox = get_bbox_mm(shape)
    if bbox is None:
        return None
    (x_min, y_min, _), (x_max, y_max, _) = bbox
    return round(min(x_max - x_min, y_max - y_min), 1)


def check_rule(model: ifcopenshell.file) -> dict:
    flights = list(model.by_type("IfcStairFlight"))
    # Only fall back to the parent stair when the model models no flights
    # at all — mixing the two would compare flight widths against whole
    # stair enclosures.
    measuring_flights = bool(flights)
    elements = flights if measuring_flights else list(model.by_type("IfcStair"))
    basis = "IfcStairFlight" if measuring_flights else "IfcStair (whole stair enclosure)"

    if not elements:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcStairFlight or IfcStair elements found in the model.",
        }

    settings = geom_settings()

    violations = []
    checked = 0
    no_geometry = 0
    implausible = 0

    for element in elements:
        width_mm = _plan_width_mm(element, settings)
        if width_mm is None:
            no_geometry += 1
            continue
        if not (MIN_PLAUSIBLE_WIDTH_MM <= width_mm <= MAX_PLAUSIBLE_WIDTH_MM):
            implausible += 1
            continue

        checked += 1
        if width_mm < MIN_STAIRWAY_WIDTH_MM:
            violations.append({
                "condition": COND_WIDTH,
                "description": "Stairway width is below the minimum required in the egress system.",
                "rule_ref": RULE_REF,
                "threshold": f">= {MIN_STAIRWAY_WIDTH_MM:.0f} mm",
                "locations": [{
                    "element": element_label(element),
                    "storey": element_storey(element),
                    "measured": (
                        f"width={width_mm:.0f} mm, required={MIN_STAIRWAY_WIDTH_MM:.0f} mm, "
                        f"measured_on={basis}"
                    ),
                }],
            })

    unknown_reasons = []
    if no_geometry:
        unknown_reasons.append({
            "condition": COND_WIDTH,
            "missing": f"body geometry on {basis}",
            "affected_elements": no_geometry,
        })

    checked_summary = {COND_WIDTH: {
        "elements_checked": checked,
        "elements_skipped": len(elements) - checked,
        "skip_reasons": {
            k: v for k, v in (
                ("missing_geometry", no_geometry),
                ("implausible_width", implausible),
            ) if v
        },
    }}

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} of {checked} measured stairway(s) are narrower than "
            f"{MIN_STAIRWAY_WIDTH_MM:.0f} mm."
        )
    elif checked == 0:
        verdict = "unknown"
        summary = (
            f"None of the {len(elements)} stair element(s) have usable geometry; "
            f"compliance cannot be determined."
        )
    else:
        verdict = "pass"
        summary = (
            f"All {checked} measured stairway(s) are at least "
            f"{MIN_STAIRWAY_WIDTH_MM:.0f} mm wide (measured on {basis})."
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
