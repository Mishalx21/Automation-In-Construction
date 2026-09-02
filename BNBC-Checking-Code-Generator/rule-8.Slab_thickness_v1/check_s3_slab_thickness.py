"""
Rule S3 — Slab thickness below minimum (ACI 318 Table 7.3.1.1).

Required minimum thickness = max(ABSOLUTE_MIN_MM, span / SPAN_COEFF).

  ABSOLUTE_MIN_MM = 100 mm   common absolute-minimum figure for a
                              structural (non-topping) floor slab.
  SPAN_COEFF      = 28        Table 7.3.1.1's coefficient for a one-way
                              slab continuous at both ends (the most
                              common condition in the corpus this
                              checker was validated against; simply
                              supported/one-end-continuous/cantilever
                              slabs need 20/24/10 respectively — not
                              distinguished here without end-condition
                              data).

Span is the slab's longest horizontal bounding-box dimension; thickness
is its vertical (Z) extent.

Usage:
    python check_s3_slab_thickness.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, geom_settings, get_bbox_mm  # noqa: E402

RULE_REF = "ACI 318 Table 7.3.1.1 (BNBC 2020 Ch.8 slab-thickness analogue)"
ABSOLUTE_MIN_MM = 100.0
SPAN_COEFF = 28.0
COND = "slab_thickness_min"


def check_rule(model: ifcopenshell.file) -> dict:
    slabs = model.by_type("IfcSlab")
    if not slabs:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcSlab elements found in the model.",
        }

    settings = geom_settings()
    violations = []
    checked = skipped = 0

    for slab in slabs:
        if getattr(slab, "PredefinedType", None) == "ROOF":
            # A sloped roof panel's axis-aligned bounding-box "vertical"
            # extent captures rise-over-run from the slope, not material
            # thickness -- even the SMALLEST of the three bbox axes can
            # still be dominated by slope for a large panel, so no bbox
            # axis reliably represents thickness here. Getting the true
            # perpendicular-to-surface thickness needs mesh-normal
            # analysis this checker doesn't have; report unknown rather
            # than silently trusting a bbox-derived number that is known
            # to be unreliable for this geometry class.
            skipped += 1
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, slab)
        except Exception:
            skipped += 1
            continue
        bbox = get_bbox_mm(shape)
        if bbox is None:
            skipped += 1
            continue

        (x0, y0, z0), (x1, y1, z1) = bbox
        span_mm = max(x1 - x0, y1 - y0)
        thickness_mm = z1 - z0
        if thickness_mm <= 0:
            skipped += 1
            continue

        checked += 1
        required_min = max(ABSOLUTE_MIN_MM, span_mm / SPAN_COEFF)
        if thickness_mm < required_min:
            violations.append({
                "condition": COND,
                "description": "Slab thickness is below the minimum required for its span.",
                "rule_ref": RULE_REF,
                "threshold": f">= max({ABSOLUTE_MIN_MM:.0f} mm, span/{SPAN_COEFF:.0f})",
                "locations": [{
                    "element": element_label(slab), "storey": element_storey(slab),
                    "measured": f"thickness={thickness_mm:.0f} mm, span={span_mm:.0f} mm, required>={required_min:.0f} mm",
                }],
            })

    unknown_reasons = []
    if skipped:
        unknown_reasons.append({
            "condition": COND, "missing": "unparseable/degenerate geometry on IfcSlab",
            "affected_elements": skipped,
        })

    checked_summary = {
        COND: {"elements_checked": checked, "elements_skipped": skipped,
               "skip_reasons": {"no_usable_geometry": skipped} if skipped else {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} of {checked} checked slab(s) are thinner than the required minimum."
    elif checked == 0:
        verdict = "unknown"
        summary = "No slab has usable geometry; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked slab(s) meet the required minimum thickness."

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
