"""
Rule A2 — Stair riser height and tread depth (IBC 1011.5.2).

Requirement:
  riser height  <= 178 mm (7 in)
  tread depth   >= 279 mm (11 in)

Source of measurement: IfcStairFlight is the primary carrier. IFC4 exposes
RiserHeight / TreadLength as direct attributes; IFC2X3 (and some IFC4
exports) only populate them via Pset_StairFlightCommon. Both are checked,
attribute first.

Fallback: several real-world exports never model IfcStairFlight sub-parts
at all and instead carry riser/tread data directly on the parent IfcStair
(via Pset_StairFlightCommon or Pset_StairCommon). Those models are checked
too, so a building with bare IfcStair-only geometry is not silently
skipped as not_applicable.

Usage:
    python check_a2_stair_riser_tread.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, length_unit_to_mm, property_sets  # noqa: E402

RULE_REF = "IBC 1011.5.2 (BNBC 2020 Ch.8 stair-geometry analogue)"
MAX_RISER_MM = 178.0
MIN_TREAD_MM = 279.0
COND_RISER = "stair_riser_max_height"
COND_TREAD = "stair_tread_min_depth"


def _get_riser_tread_mm(element: ifcopenshell.entity_instance, unit_mm: float):
    riser = getattr(element, "RiserHeight", None)
    tread = getattr(element, "TreadLength", None)

    if riser is None or tread is None:
        psets = property_sets(element)
        # IfcStairFlight is documented as Pset_StairFlightCommon; some
        # real-world exports instead put the same properties directly on
        # the parent IfcStair, and a few label that pset Pset_StairCommon.
        for pset_name in ("Pset_StairFlightCommon", "Pset_StairCommon"):
            common = psets.get(pset_name, {})
            if riser is None:
                riser = common.get("RiserHeight")
            if tread is None:
                tread = common.get("TreadLength")

    riser_mm = float(riser) * unit_mm if riser is not None else None
    tread_mm = float(tread) * unit_mm if tread is not None else None
    return riser_mm, tread_mm


def check_rule(model: ifcopenshell.file) -> dict:
    flights = list(model.by_type("IfcStairFlight"))
    # Fallback: some real exports never model IfcStairFlight sub-parts at
    # all and carry riser/tread data directly on the parent IfcStair.
    stairs = list(model.by_type("IfcStair"))
    flights = flights + stairs

    if not flights:
        return {
            "verdict": "not_applicable",
            "violations": [],
            "violation_count": 0,
            "unknown_reasons": [],
            "checked_summary": {},
            "summary": "No IfcStairFlight or IfcStair elements found in the model.",
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checked = {COND_RISER: 0, COND_TREAD: 0}
    skipped = {COND_RISER: 0, COND_TREAD: 0}

    for flight in flights:
        riser_mm, tread_mm = _get_riser_tread_mm(flight, unit_mm)
        label = element_label(flight)
        storey = element_storey(flight)

        if riser_mm is None:
            skipped[COND_RISER] += 1
        else:
            checked[COND_RISER] += 1
            if riser_mm > MAX_RISER_MM:
                violations.append({
                    "condition": COND_RISER,
                    "description": "Stair riser height exceeds the maximum allowed.",
                    "rule_ref": RULE_REF,
                    "threshold": f"<= {MAX_RISER_MM:.0f} mm",
                    "locations": [{
                        "element": label, "storey": storey,
                        "measured": f"riser={riser_mm:.1f} mm, required<={MAX_RISER_MM:.0f} mm",
                    }],
                })

        if tread_mm is None:
            skipped[COND_TREAD] += 1
        else:
            checked[COND_TREAD] += 1
            if tread_mm < MIN_TREAD_MM:
                violations.append({
                    "condition": COND_TREAD,
                    "description": "Stair tread depth is below the minimum required.",
                    "rule_ref": RULE_REF,
                    "threshold": f">= {MIN_TREAD_MM:.0f} mm",
                    "locations": [{
                        "element": label, "storey": storey,
                        "measured": f"tread={tread_mm:.1f} mm, required>={MIN_TREAD_MM:.0f} mm",
                    }],
                })

    unknown_reasons = []
    for cond, label in ((COND_RISER, "RiserHeight"), (COND_TREAD, "TreadLength")):
        if skipped[cond]:
            unknown_reasons.append({
                "condition": cond,
                "missing": f"{label} on IfcStairFlight (attribute or Pset_StairFlightCommon)",
                "affected_elements": skipped[cond],
            })

    checked_summary = {
        cond: {
            "elements_checked": checked[cond],
            "elements_skipped": skipped[cond],
            "skip_reasons": {"missing_data": skipped[cond]} if skipped[cond] else {},
        }
        for cond in (COND_RISER, COND_TREAD)
    }

    total_checked = checked[COND_RISER] + checked[COND_TREAD]
    if violations:
        verdict = "fail"
        summary = f"{len(violations)} stair-flight condition(s) violate riser/tread limits."
    elif total_checked == 0:
        verdict = "unknown"
        summary = "No stair flight has usable riser/tread data; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {len(flights)} stair flight(s) meet riser<= {MAX_RISER_MM:.0f}mm / tread>= {MIN_TREAD_MM:.0f}mm."

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
