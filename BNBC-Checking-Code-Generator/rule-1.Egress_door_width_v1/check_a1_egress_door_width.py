"""
Rule A1 — Egress door clear width (IBC 1010.1.1 / BNBC 2020 analogue).

Requirement: every egress door must provide a clear width >= 815 mm
(32 in). Below that, the door itself is the bottleneck for evacuation
flow and wheelchair passage.

Convention adopted (no frame-reduction geometry available from IFC in
general): IfcDoor.OverallWidth is treated as the clear opening width.
This is the same simplification used by most authoring-tool exports —
OverallWidth is populated from the door leaf's nominal clear size, not
the rough/frame size. Doors without a usable OverallWidth are reported
as "unknown" rather than silently skipped or passed.

Usage:
    python check_a1_egress_door_width.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, length_unit_to_mm  # noqa: E402

RULE_ID = "A1"
RULE_REF = "IBC 1010.1.1 (BNBC 2020 Ch.8 egress-width analogue)"
MIN_CLEAR_WIDTH_MM = 815.0
CONDITION_ID = "egress_door_min_clear_width"


def check_rule(model: ifcopenshell.file) -> dict:
    doors = model.by_type("IfcDoor")

    if not doors:
        return {
            "verdict": "not_applicable",
            "violations": [],
            "violation_count": 0,
            "unknown_reasons": [],
            "checked_summary": {},
            "summary": "No IfcDoor elements found in the model.",
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checked = 0
    skipped = 0
    unknown_elements = 0

    for door in doors:
        width_raw = getattr(door, "OverallWidth", None)
        if width_raw is None:
            skipped += 1
            unknown_elements += 1
            continue

        width_mm = float(width_raw) * unit_mm
        checked += 1

        if width_mm < MIN_CLEAR_WIDTH_MM:
            violations.append(
                {
                    "condition": CONDITION_ID,
                    "description": "Door clear width is below the minimum required for egress.",
                    "rule_ref": RULE_REF,
                    "threshold": f">= {MIN_CLEAR_WIDTH_MM:.0f} mm",
                    "locations": [
                        {
                            "element": element_label(door),
                            "storey": element_storey(door),
                            "measured": f"width={width_mm:.1f} mm, required>={MIN_CLEAR_WIDTH_MM:.0f} mm",
                        }
                    ],
                }
            )

    unknown_reasons = []
    if unknown_elements:
        unknown_reasons.append(
            {
                "condition": CONDITION_ID,
                "missing": "OverallWidth on IfcDoor",
                "affected_elements": unknown_elements,
            }
        )

    checked_summary = {
        CONDITION_ID: {
            "elements_checked": checked,
            "elements_skipped": skipped,
            "skip_reasons": {"missing_OverallWidth": skipped} if skipped else {},
        }
    }

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} of {checked} checked door(s) have clear width "
            f"below {MIN_CLEAR_WIDTH_MM:.0f} mm."
        )
    elif checked == 0:
        verdict = "unknown"
        summary = "All doors are missing OverallWidth; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked door(s) meet the {MIN_CLEAR_WIDTH_MM:.0f} mm minimum clear width."

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

    ifc_path = Path(sys.argv[1])
    model = ifcopenshell.open(str(ifc_path))
    result = check_rule(model)

    print(json.dumps(result, indent=2))
    print()
    print(f"Verdict: {result['verdict'].upper()}")
    if result["violations"]:
        print(f"Violations ({result['violation_count']}):")
        for v in result["violations"]:
            for loc in v["locations"]:
                print(f"  - {loc['element']} @ {loc['storey']}: {loc['measured']}")

    return 1 if result["verdict"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
