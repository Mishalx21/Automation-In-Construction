"""
Rule S4 — Discontinuous or floating column
(ASCE 7 Table 12.3-2 / EC8 4.2.3.3).

A column must be continuously supported down to the foundation: at the
storey directly below it there must be another column (continuing the
load path) or, at the lowest storey, a footing (IfcFooting) beneath it.
Support is tested as XY-footprint (plan) bounding-box overlap between
the column and the candidate support element, independent of storey
ordering ambiguity by using IfcBuildingStorey.Elevation to sort storeys.

A column with no supporting column/footing directly below it — a
"floating" column — is the classic progressive-collapse initiator this
rule targets.

Usage:
    python check_s4_floating_column.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, geom_settings, get_bbox_mm  # noqa: E402

RULE_REF = "ASCE 7 Table 12.3-2 / EC8 4.2.3.3 (BNBC 2020 Ch.8 load-path-continuity analogue)"
COND = "column_continuous_support"


def _storey_order(model: ifcopenshell.file) -> dict:
    storeys = model.by_type("IfcBuildingStorey")
    ordered = sorted(storeys, key=lambda s: (getattr(s, "Elevation", None) is None, getattr(s, "Elevation", 0.0) or 0.0))
    return {s.Name or f"#{s.id()}": i for i, s in enumerate(ordered)}


def _footprint(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    bbox = get_bbox_mm(shape)
    if bbox is None:
        return None
    (x0, y0, _), (x1, y1, _) = bbox
    return (x0, y0, x1, y1)


def _overlaps_xy(a, b) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 <= bx1 and ax1 >= bx0 and ay0 <= by1 and ay1 >= by0


def check_rule(model: ifcopenshell.file) -> dict:
    columns = model.by_type("IfcColumn")
    footings = model.by_type("IfcFooting")

    if not columns:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcColumn elements found in the model.",
            "checks": [],
        }

    settings = geom_settings()
    storey_rank = _storey_order(model)

    by_storey: dict[str, list] = {}
    for col in columns:
        storey = element_storey(col)
        by_storey.setdefault(storey, []).append(col)

    footing_footprints = [(f, _footprint(settings, f)) for f in footings]
    footing_footprints = [(f, fp) for f, fp in footing_footprints if fp is not None]

    violations = []
    checks = []
    checked = skipped = 0

    for storey, cols in by_storey.items():
        rank = storey_rank.get(storey)
        below_storey = None
        if rank is not None:
            for name, r in storey_rank.items():
                if r == rank - 1:
                    below_storey = name
                    break

        below_columns = by_storey.get(below_storey, []) if below_storey else []
        below_footprints = [(c, _footprint(settings, c)) for c in below_columns]
        below_footprints = [(c, fp) for c, fp in below_footprints if fp is not None]

        is_lowest = rank == 0 or below_storey is None

        for col in cols:
            fp = _footprint(settings, col)
            if fp is None:
                skipped += 1
                continue
            checked += 1

            supported = any(_overlaps_xy(fp, ofp) for _, ofp in below_footprints)
            if not supported and is_lowest:
                supported = any(_overlaps_xy(fp, ofp) for _, ofp in footing_footprints)

            threshold = "must be supported by a column or footing directly below"
            is_violation = not supported
            if is_violation:
                reason = "no footing beneath it" if is_lowest else f"no column beneath it at '{below_storey}'"
                measured = f"support_found=False ({reason})"
            else:
                measured = "support_found=True"
            checks.append({
                "element": element_label(col),
                "storey": storey,
                "measured": measured,
                "threshold": threshold,
                "result": "fail" if is_violation else "pass",
            })
            if is_violation:
                violations.append({
                    "condition": COND,
                    "description": f"Column has {reason} — the load path is discontinuous.",
                    "rule_ref": RULE_REF,
                    "threshold": threshold,
                    "locations": [{
                        "element": element_label(col), "storey": storey,
                        "measured": measured,
                    }],
                })

    unknown_reasons = []
    if skipped:
        unknown_reasons.append({
            "condition": COND, "missing": "unparseable/degenerate geometry on IfcColumn",
            "affected_elements": skipped,
        })

    checked_summary = {
        COND: {"elements_checked": checked, "elements_skipped": skipped,
               "skip_reasons": {"no_usable_geometry": skipped} if skipped else {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} of {checked} checked column(s) are discontinuous/floating."
    elif checked == 0:
        verdict = "unknown"
        summary = "No column has usable geometry; compliance cannot be determined."
    else:
        verdict = "pass"
        summary = f"All {checked} checked column(s) have continuous support down to a footing."

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
