"""
Rule S2 — Column minimum dimension and slenderness
(EC8 5.4.1.2.1 / EC2 5.8.3.1).

Two conditions, both derived from the column's bounding box:

  column_min_dimension
      Least horizontal cross-section dimension must be >= 200 mm (a
      common seismic-detailing minimum column dimension; EC8
      5.4.1.2.1 ties the exact figure to ductility class, 200 mm is
      the conservative low end used here).

  column_slenderness_max
      clear height / least horizontal dimension must be <= 15 (a
      conservative simplified stand-in for EC2 5.8.3.1's
      lambda_lim formula, which needs axial-load and end-restraint
      data not reliably present in IFC geometry alone).

Usage:
    python check_s2_column_dimension_slenderness.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, geom_settings, get_bbox_mm  # noqa: E402

RULE_REF = "EC8 5.4.1.2.1 / EC2 5.8.3.1 (BNBC 2020 Ch.8 column-stability analogue)"
MIN_DIMENSION_MM = 200.0
MAX_SLENDERNESS = 15.0
COND_DIM = "column_min_dimension"
COND_SLEND = "column_slenderness_max"


def check_rule(model: ifcopenshell.file) -> dict:
    columns = model.by_type("IfcColumn")
    if not columns:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcColumn elements found in the model.",
            "checks": [],
        }

    settings = geom_settings()
    violations = []
    checks = []
    checked = skipped = 0

    for column in columns:
        try:
            shape = ifcopenshell.geom.create_shape(settings, column)
        except Exception:
            skipped += 1
            continue
        bbox = get_bbox_mm(shape)
        if bbox is None:
            skipped += 1
            continue

        (x0, y0, z0), (x1, y1, z1) = bbox
        least_dim = min(x1 - x0, y1 - y0)
        height = z1 - z0
        if least_dim <= 0 or height <= 0:
            skipped += 1
            continue

        checked += 1
        label = element_label(column)
        storey = element_storey(column)

        dim_threshold = f">= {MIN_DIMENSION_MM:.0f} mm"
        dim_measured = f"least_dimension={least_dim:.0f} mm, required>={MIN_DIMENSION_MM:.0f} mm"
        dim_is_violation = least_dim < MIN_DIMENSION_MM
        checks.append({
            "element": label,
            "storey": storey,
            "criterion": "Minimum dimension",
            "measured": dim_measured,
            "threshold": dim_threshold,
            "result": "fail" if dim_is_violation else "pass",
        })
        if dim_is_violation:
            violations.append({
                "condition": COND_DIM,
                "description": "Column least cross-section dimension is below the minimum required.",
                "rule_ref": RULE_REF,
                "threshold": dim_threshold,
                "locations": [{
                    "element": label, "storey": storey,
                    "measured": dim_measured,
                }],
            })

        slenderness = height / least_dim
        slend_threshold = f"<= {MAX_SLENDERNESS:.0f}"
        slend_measured = f"height={height:.0f} mm, least_dimension={least_dim:.0f} mm, ratio={slenderness:.1f}"
        slend_is_violation = slenderness > MAX_SLENDERNESS
        checks.append({
            "element": label,
            "storey": storey,
            "criterion": "Slenderness ratio",
            "measured": slend_measured,
            "threshold": slend_threshold,
            "result": "fail" if slend_is_violation else "pass",
        })
        if slend_is_violation:
            violations.append({
                "condition": COND_SLEND,
                "description": "Column slenderness ratio (height / least dimension) exceeds the limit.",
                "rule_ref": RULE_REF,
                "threshold": slend_threshold,
                "locations": [{
                    "element": label, "storey": storey,
                    "measured": slend_measured,
                }],
            })

    unknown_reasons = []
    if skipped:
        unknown_reasons.append({
            "condition": COND_DIM, "missing": "unparseable/degenerate geometry on IfcColumn",
            "affected_elements": skipped,
        })

    checked_summary = {
        COND_DIM: {"elements_checked": checked, "elements_skipped": skipped,
                   "skip_reasons": {"no_usable_geometry": skipped} if skipped else {}},
        COND_SLEND: {"elements_checked": checked, "elements_skipped": skipped,
                     "skip_reasons": {"no_usable_geometry": skipped} if skipped else {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} column condition(s) violate minimum-dimension/slenderness limits."
    elif checked == 0:
        verdict = "unknown"
        summary = "No column has usable geometry; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked column(s) meet dimension>= {MIN_DIMENSION_MM:.0f}mm and slenderness<= {MAX_SLENDERNESS:.0f}."

    return {
        "verdict": verdict, "violations": violations, "violation_count": len(violations),
        "unknown_reasons": unknown_reasons, "checked_summary": checked_summary, "summary": summary,
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
