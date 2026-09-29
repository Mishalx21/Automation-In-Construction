"""
Rule S9 — Re-entrant corner plan irregularity
(BNBC 2020 Part 6 Table 6.1.5, Plan Irregularity Type II).

Requirement: "Plan configurations of a structure and its lateral force-
resisting system contain reentrant corners, where both projections of the
structure beyond a reentrant corner are greater than 15 percent of the plan
dimension of the structure in the given direction."

This is the L-, T-, U- and cross-shaped plan of BNBC Figure 6.2.28(b), where
the inside corner concentrates stress because the two wings of the building
pull against each other in an earthquake.

Conventions adopted:

* The plan. IFC carries no structural plan outline, so the storey plan is
  reconstructed from where the lateral force-resisting elements actually
  are: IfcColumn placements plus IfcWall axis lines, sampled along their
  length so a long wall fills the plan rather than marking one point. Walls
  are restricted to load-bearing ones where the model flags them, the same
  proxy S5 and S8 use.

* From scatter to outline. Those points are rasterised onto a grid spanning
  the storey's bounding rectangle, but a structural grid only ever fills
  10-40% of its own cells — the space between columns is floor, not void —
  so the raw pattern cannot be tested for notches directly. Each grid row is
  therefore filled between its leftmost and rightmost occupied cell, each
  column between its lowest and highest, and the storey's plan is taken as
  the intersection of the two. A rectangular column grid fills completely;
  an L-, T- or U-shaped one leaves precisely the missing wing empty.

* The test. At each of the four corners the largest empty rectangle anchored
  in that corner is measured against the filled outline; that empty block is
  the notch, and its two sides are the projections the clause speaks of. A
  corner is reported only when BOTH sides exceed 15% of the corresponding
  plan dimension, exactly as the clause requires — one long thin notch is
  not a re-entrant corner.

* Resolution and noise. The grid is GRID_CELLS cells on each side, so the
  measurement resolves to 5% of the plan dimension, comfortably finer than
  the 15% limit. A single unmodelled corner column changes nothing, because
  the row and column fills span past it. A storey whose filled outline
  covers too little of its bounding rectangle is reported as skipped rather
  than measured.

* Known limitation: a genuinely non-rectangular but convex plan (a chamfered
  or splayed corner) leaves one large triangular empty corner, which this
  test can report as re-entrant. Reading a plan outline from where the
  columns are cannot distinguish the two.

Usage:
    python check_s9_reentrant_corner.py <path-to-ifc>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.placement as placement_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import length_unit_to_mm, property_sets  # noqa: E402

RULE_ID = "S9"
RULE_REF = "BNBC 2020 Part 6 Table 6.1.5, Plan Irregularity Type II"
MAX_PROJECTION_FRACTION = 0.15
COND_REENTRANT = "plan_reentrant_corner"

GRID_CELLS = 20
MIN_ELEMENTS_PER_STOREY = 8
# The filled outline has to cover a real share of its own bounding rectangle
# before its empty corners mean anything.
MIN_FILLED_FRACTION = 0.35
# Below this the "plan" is a single bay, not a building footprint.
MIN_PLAUSIBLE_EXTENT_MM = 5000.0
# Wall axes are sampled at least this finely relative to the cell size, so a
# long wall marks every cell it crosses instead of only its ends.
AXIS_SAMPLES_PER_CELL = 2


def _is_load_bearing(wall) -> bool | None:
    value = property_sets(wall).get("Pset_WallCommon", {}).get("LoadBearing")
    return None if value is None else bool(value)


def _placement_matrix(element):
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        return placement_util.get_local_placement(placement)
    except Exception:
        return None


def _axis_points(element):
    """Local 2D points of the element's Axis polyline, if it has one."""
    representation = getattr(element, "Representation", None)
    if representation is None:
        return []
    for rep in getattr(representation, "Representations", None) or []:
        if getattr(rep, "RepresentationIdentifier", None) != "Axis":
            continue
        for item in getattr(rep, "Items", None) or []:
            if item.is_a("IfcPolyline"):
                return [tuple(p.Coordinates[:2]) for p in item.Points]
    return []


def _plan_points_mm(element, unit_mm: float, sample_step_mm: float):
    """World plan points describing where this element sits, in mm.

    A column is one point. A wall is its axis line, sampled along its length
    so that it occupies every grid cell it actually crosses.
    """
    matrix = _placement_matrix(element)
    if matrix is None:
        return []

    def to_world(x, y):
        wx = matrix[0][0] * x + matrix[0][1] * y + matrix[0][3]
        wy = matrix[1][0] * x + matrix[1][1] * y + matrix[1][3]
        return wx * unit_mm, wy * unit_mm

    local = _axis_points(element)
    if len(local) < 2:
        return [to_world(0.0, 0.0)]

    points = []
    for (x0, y0), (x1, y1) in zip(local, local[1:]):
        start = to_world(x0, y0)
        end = to_world(x1, y1)
        length = math.dist(start, end)
        steps = max(1, int(length / sample_step_mm) + 1) if sample_step_mm > 0 else 1
        for i in range(steps + 1):
            t = i / steps
            points.append((
                start[0] + (end[0] - start[0]) * t,
                start[1] + (end[1] - start[1]) * t,
            ))
    return points or [to_world(0.0, 0.0)]


def _storey_of(model, element):
    for rel in model.get_inverse(element):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            structure = getattr(rel, "RelatingStructure", None)
            if structure is not None and structure.is_a("IfcBuildingStorey"):
                return structure
    return None


def _filled_outline(occupied, nx, ny):
    """Turn a scatter of occupied cells into a filled plan outline.

    Each row is filled between its extreme occupied cells and each column
    likewise; the plan is where both agree. This is what separates the void
    outside an L-shaped building from the ordinary floor space between two
    columns, which is just as empty in the raw scatter.
    """
    row_span: dict[int, tuple[int, int]] = {}
    col_span: dict[int, tuple[int, int]] = {}
    for i, j in occupied:
        lo, hi = row_span.get(j, (i, i))
        row_span[j] = (min(lo, i), max(hi, i))
        lo, hi = col_span.get(i, (j, j))
        col_span[i] = (min(lo, j), max(hi, j))

    filled = set()
    for j, (i_lo, i_hi) in row_span.items():
        for i in range(i_lo, i_hi + 1):
            span = col_span.get(i)
            if span is not None and span[0] <= j <= span[1]:
                filled.add((i, j))
    return filled


def _corner_notch(occupied, nx, ny, flip_x: bool, flip_y: bool):
    """Largest empty rectangle anchored at one corner, as (cells_x, cells_y).

    Walks outward from the corner column by column. `run` is how far the
    empty block can still reach in the other direction once this column is
    included, so the answer is always a true rectangle.
    """
    def is_empty(i, j):
        x = (nx - 1 - i) if flip_x else i
        y = (ny - 1 - j) if flip_y else j
        return (x, y) not in occupied

    best = (0, 0)
    run = ny
    for i in range(nx):
        depth = 0
        while depth < ny and is_empty(i, depth):
            depth += 1
        run = min(run, depth)
        if run == 0:
            break
        if (i + 1) * run > best[0] * best[1]:
            best = (i + 1, run)
    return best


def check_rule(model: ifcopenshell.file) -> dict:
    storeys = [s for s in model.by_type("IfcBuildingStorey")]
    columns = list(model.by_type("IfcColumn"))
    walls = list(model.by_type("IfcWall"))

    if not storeys:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {}, "checks": [],
            "summary": "No IfcBuildingStorey elements found in the model.",
        }
    if not columns and not walls:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {}, "checks": [],
            "summary": (
                "No IfcColumn or IfcWall elements to stand for the lateral "
                "force-resisting system."
            ),
        }

    unit_mm = length_unit_to_mm(model)

    wall_flags = [_is_load_bearing(w) for w in walls]
    any_flagged = any(flag is True for flag in wall_flags)
    lfrs = list(columns) + [
        wall for wall, flag in zip(walls, wall_flags) if not any_flagged or flag is True
    ]

    # storey -> its lateral elements
    by_storey: dict[str, list] = {}
    storey_by_gid = {s.GlobalId: s for s in storeys}
    unplaced = 0
    for element in lfrs:
        storey = _storey_of(model, element)
        if storey is None:
            unplaced += 1
            continue
        by_storey.setdefault(storey.GlobalId, []).append(element)

    violations = []
    checks = []
    checked = 0
    too_few = 0
    too_sparse = 0
    too_small = 0

    for storey_gid, elements in sorted(by_storey.items()):
        storey = storey_by_gid.get(storey_gid)
        if storey is None or len(elements) < MIN_ELEMENTS_PER_STOREY:
            too_few += 1
            continue

        # A first pass on placements alone fixes the extent, which in turn
        # fixes the cell size used to sample wall axes.
        origins = []
        for element in elements:
            matrix = _placement_matrix(element)
            if matrix is not None:
                origins.append((float(matrix[0][3]) * unit_mm, float(matrix[1][3]) * unit_mm))
        if len(origins) < MIN_ELEMENTS_PER_STOREY:
            too_few += 1
            continue

        x_min = min(p[0] for p in origins)
        x_max = max(p[0] for p in origins)
        y_min = min(p[1] for p in origins)
        y_max = max(p[1] for p in origins)
        width = x_max - x_min
        depth = y_max - y_min
        if width < MIN_PLAUSIBLE_EXTENT_MM or depth < MIN_PLAUSIBLE_EXTENT_MM:
            too_small += 1
            continue

        cell_x = width / GRID_CELLS
        cell_y = depth / GRID_CELLS
        sample_step = max(min(cell_x, cell_y) / AXIS_SAMPLES_PER_CELL, 1.0)

        occupied: set[tuple[int, int]] = set()
        for element in elements:
            for px, py in _plan_points_mm(element, unit_mm, sample_step):
                i = min(GRID_CELLS - 1, max(0, int((px - x_min) / cell_x))) if cell_x else 0
                j = min(GRID_CELLS - 1, max(0, int((py - y_min) / cell_y))) if cell_y else 0
                occupied.add((i, j))

        filled = _filled_outline(occupied, GRID_CELLS, GRID_CELLS)
        if len(filled) < MIN_FILLED_FRACTION * GRID_CELLS * GRID_CELLS:
            too_sparse += 1
            continue

        checked += 1
        storey_name = str(storey.Name or storey.GlobalId)
        corners = (
            ("south-west", False, False),
            ("south-east", True, False),
            ("north-west", False, True),
            ("north-east", True, True),
        )
        for corner_name, flip_x, flip_y in corners:
            cells_x, cells_y = _corner_notch(filled, GRID_CELLS, GRID_CELLS, flip_x, flip_y)
            fraction_x = cells_x / GRID_CELLS
            fraction_y = cells_y / GRID_CELLS
            is_violation = fraction_x > MAX_PROJECTION_FRACTION and fraction_y > MAX_PROJECTION_FRACTION
            threshold = f"<= {MAX_PROJECTION_FRACTION:.0%} of the plan dimension in one direction"
            measured = (
                f"corner={corner_name}, "
                f"projection_x={fraction_x:.0%} of {width:.0f} mm, "
                f"projection_y={fraction_y:.0%} of {depth:.0f} mm, "
                f"limit={MAX_PROJECTION_FRACTION:.0%}"
            )
            checks.append({
                "element": f"Storey '{storey_name}'",
                "storey": storey_name,
                "criterion": f"Re-entrant corner ({corner_name})",
                "measured": measured,
                "threshold": threshold,
                "result": "fail" if is_violation else "pass",
            })
            if not is_violation:
                continue
            violations.append({
                "condition": COND_REENTRANT,
                "description": (
                    "The storey plan has a re-entrant corner whose projections exceed "
                    "15% of the plan dimension in both directions."
                ),
                "rule_ref": RULE_REF,
                "threshold": threshold,
                "locations": [{
                    "element": f"Storey '{storey_name}'",
                    "storey": storey_name,
                    "measured": measured,
                }],
            })

    unknown_reasons = []
    if unplaced:
        unknown_reasons.append({
            "condition": COND_REENTRANT,
            "missing": "storey containment on a lateral element",
            "affected_elements": unplaced,
        })
    if too_few or too_sparse:
        unknown_reasons.append({
            "condition": COND_REENTRANT,
            "missing": (
                f"enough placed lateral elements to describe a plan outline "
                f"(at least {MIN_ELEMENTS_PER_STOREY} elements, filling "
                f"{MIN_FILLED_FRACTION:.0%} of the storey's bounding rectangle)"
            ),
            "affected_elements": too_few + too_sparse,
        })

    checked_summary = {COND_REENTRANT: {
        "elements_checked": checked,
        "elements_skipped": len(by_storey) - checked,
        "skip_reasons": {
            k: v for k, v in (
                ("too_few_lateral_elements", too_few),
                ("plan_outline_too_sparse", too_sparse),
                ("plan_too_small", too_small),
            ) if v
        },
    }}

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} re-entrant corner(s) across {checked} checked storey plan(s) "
            f"exceed {MAX_PROJECTION_FRACTION:.0%} in both directions."
        )
    elif checked == 0:
        verdict = "unknown"
        summary = (
            "No storey has enough placed lateral elements to reconstruct a plan "
            "outline; compliance cannot be determined."
        )
    else:
        verdict = "pass"
        summary = (
            f"All {checked} checked storey plan(s) are free of re-entrant corners beyond "
            f"{MAX_PROJECTION_FRACTION:.0%} in both directions."
        )

    return {
        "verdict": verdict,
        "violations": violations,
        "violation_count": len(violations),
        "unknown_reasons": unknown_reasons,
        "checked_summary": checked_summary,
        "summary": summary,
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
