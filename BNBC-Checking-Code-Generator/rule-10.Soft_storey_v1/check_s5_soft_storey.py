"""
Rule S5 — Soft storey (ASCE 7 / EC8 4.2.3.3).

A soft storey is one with markedly less lateral stiffness than the
storeys above/below it. Full stiffness analysis needs a structural
model this checker doesn't have; the standard IFC-only proxy is to
compare the count of load-bearing walls per storey (Pset_WallCommon.
LoadBearing = True; if no wall anywhere carries that flag, every
IfcWall is used as a fallback proxy) against the median across all
storeys. A storey whose load-bearing wall count drops below
SOFT_STOREY_RATIO of the building's median is flagged — this is the
dominant, most literal IFC-observable signature of the rule's own
fixture ("delete the shear walls on that storey").

Usage:
    python check_s5_soft_storey.py <path-to-ifc>
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import element_label, element_storey, property_sets  # noqa: E402

RULE_REF = "ASCE 7 / EC8 4.2.3.3 (BNBC 2020 Ch.8 lateral-stiffness-irregularity analogue)"
SOFT_STOREY_RATIO = 0.3
COND = "storey_lateral_wall_deficit"


def _is_load_bearing(wall) -> bool | None:
    common = property_sets(wall).get("Pset_WallCommon", {})
    val = common.get("LoadBearing")
    if val is None:
        return None
    return bool(val)


def check_rule(model: ifcopenshell.file) -> dict:
    walls = model.by_type("IfcWall")
    storeys = model.by_type("IfcBuildingStorey")

    if not storeys:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcBuildingStorey elements found in the model.",
        }

    if not walls:
        # A building with defined storeys but literally zero IfcWall anywhere
        # has no IFC-observable lateral wall system at all. This is NOT
        # "out of scope" -- it is the most severe form of this rule's own
        # signature (total loss of wall-based lateral bracing is worse than
        # an ordinary single-storey deficit), so it must be surfaced as a
        # violation rather than silently absorbed into not_applicable.
        violations = [{
            "condition": COND,
            "description": (
                "No load-bearing walls found anywhere in the model -- total loss of "
                "the wall-based lateral system, a more severe case than an ordinary "
                "single-storey wall deficit."
            ),
            "rule_ref": RULE_REF,
            "threshold": "> 0 load-bearing walls expected somewhere in the building",
            "locations": [{
                "element": f"Storey '{s.Name or f'#{s.id()}'}'",
                "storey": s.Name or f"#{s.id()}",
                "measured": "load_bearing_walls=0 (building-wide)",
            } for s in storeys],
        }]
        return {
            "verdict": "fail", "violations": violations, "violation_count": len(violations),
            "unknown_reasons": [],
            "checked_summary": {COND: {"elements_checked": len(storeys), "elements_skipped": 0, "skip_reasons": {}}},
            "summary": f"No IfcWall found anywhere across {len(storeys)} storey(s) -- total lateral wall-system loss.",
        }

    flags = [_is_load_bearing(w) for w in walls]
    any_flagged = any(f is True for f in flags)

    counts: dict[str, int] = {s.Name or f"#{s.id()}": 0 for s in storeys}
    for wall, flag in zip(walls, flags):
        if any_flagged and flag is not True:
            continue
        storey = element_storey(wall)
        counts[storey] = counts.get(storey, 0) + 1

    values = list(counts.values())
    if not values or all(v == 0 for v in values):
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No load-bearing walls found on any storey; condition out of scope.",
        }

    median_count = statistics.median(values)
    threshold = median_count * SOFT_STOREY_RATIO

    violations = []
    for storey, count in counts.items():
        if median_count > 0 and count < threshold:
            violations.append({
                "condition": COND,
                "description": "Storey has far fewer load-bearing walls than the building median — a soft-storey signature.",
                "rule_ref": RULE_REF,
                "threshold": f">= {threshold:.1f} walls ({SOFT_STOREY_RATIO:.0%} of median {median_count:.1f})",
                "locations": [{
                    "element": f"Storey '{storey}'", "storey": storey,
                    "measured": f"load_bearing_walls={count}, building_median={median_count:.1f}",
                }],
            })

    checked_summary = {
        COND: {"elements_checked": len(counts), "elements_skipped": 0, "skip_reasons": {}},
    }

    if violations:
        verdict = "fail"
        summary = f"{len(violations)} storey(s) show a soft-storey wall deficit."
    else:
        verdict = "pass"
        summary = f"All {len(counts)} storey(s) have a comparable load-bearing wall count (median={median_count:.1f})."

    return {
        "verdict": verdict, "violations": violations, "violation_count": len(violations),
        "unknown_reasons": [], "checked_summary": checked_summary, "summary": summary,
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
