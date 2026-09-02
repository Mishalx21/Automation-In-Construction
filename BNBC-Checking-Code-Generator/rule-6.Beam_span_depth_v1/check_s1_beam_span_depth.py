"""
Rule S1 — Beam span-to-depth ratio (EC2 7.4.2 / ACI 318 Table 9.3.1.1).

Requirement: span / depth <= 20 for a simply-supported beam (the
standard deflection-control rule-of-thumb both codes converge on for
the simply-supported case; continuous/cantilever beams have looser and
tighter limits respectively, but this checker only implements the
simply-supported figure, matching the baseline models it was built
against).

Span is taken as the beam's longest horizontal bounding-box dimension;
depth as its vertical (Z) bounding-box extent. Both derived from
geometry rather than profile attributes, so tapered/non-prismatic
beams are measured at their overall envelope, not their true section.

Usage:
    python check_s1_beam_span_depth.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, geom_settings, get_bbox_mm  # noqa: E402

RULE_REF = "EC2 7.4.2 / ACI 318 Table 9.3.1.1 (BNBC 2020 Ch.8 deflection-control analogue)"
MAX_SPAN_TO_DEPTH = 20.0
COND = "beam_span_to_depth_max"


def check_rule(model: ifcopenshell.file) -> dict:
    beams = model.by_type("IfcBeam")
    if not beams:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcBeam elements found in the model.",
        }

    settings = geom_settings()
    violations = []
    checked = skipped = 0

    for beam in beams:
        try:
            shape = ifcopenshell.geom.create_shape(settings, beam)
        except Exception:
            skipped += 1
            continue
        bbox = get_bbox_mm(shape)
        if bbox is None:
            skipped += 1
            continue

        (x0, y0, z0), (x1, y1, z1) = bbox
        span_mm = max(x1 - x0, y1 - y0)
        depth_mm = z1 - z0
        if depth_mm <= 0:
            skipped += 1
            continue

        checked += 1
        ratio = span_mm / depth_mm
        if ratio > MAX_SPAN_TO_DEPTH:
            violations.append({
                "condition": COND,
                "description": "Beam span-to-depth ratio exceeds the deflection-control limit.",
                "rule_ref": RULE_REF,
                "threshold": f"<= {MAX_SPAN_TO_DEPTH:.0f}",
                "locations": [{
                    "element": element_label(beam), "storey": element_storey(beam),
                    "measured": f"span={span_mm:.0f} mm, depth={depth_mm:.0f} mm, L/d={ratio:.1f}",
                }],
            })

    unknown_reasons = []
    if skipped:
        unknown_reasons.append({
            "condition": COND, "missing": "unparseable/degenerate geometry on IfcBeam",
            "affected_elements": skipped,
        })

    checked_summary = {
        COND: {"elements_checked": checked, "elements_skipped": skipped,
               "skip_reasons": {"no_usable_geometry": skipped} if skipped else {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} of {checked} checked beam(s) exceed L/d <= {MAX_SPAN_TO_DEPTH:.0f}."
    elif checked == 0:
        verdict = "unknown"
        summary = "No beam has usable geometry; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked beam(s) meet L/d <= {MAX_SPAN_TO_DEPTH:.0f}."

    return {
        "verdict": verdict, "violations": violations, "violation_count": len(violations),
        "unknown_reasons": unknown_reasons, "checked_summary": checked_summary, "summary": summary,
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
