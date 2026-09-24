"""
S9  - re-entrant corner: a rectangular storey plan turned into an L by
removing one corner, far enough in both directions to be a plan irregularity
under BNBC Table 6.1.5 Type II.

Mechanism: delete the lateral elements that fall inside one corner block of
a storey's plan, leaving the two wings of the L intact. Because only the
INTERSECTION of the two outer bands is removed, the elements holding the
storey's extreme X and extreme Y both survive: the bounding rectangle is
unchanged and the corner is genuinely notched out of it, which is exactly
the shape the clause describes.

The block is cut at NOTCH_FRACTION of the plan in each direction, comfortably
past the clause's 15%, so the defect survives a checker measuring the plan
on a coarser grid than this module does.

A corner is only offered when the storey does not already have a notch there
(otherwise the "injected" defect was in the model to begin with) and when
enough of the plan survives for the outline to still be readable.
"""
from __future__ import annotations

import math

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import delete_element
from .helpers import (
    global_xyz_mm, length_unit_scale, prop_value, psets_of, sorted_by_guid, storey_of,
)

RULE_ID = "S9"
CLAUSE = ("BNBC 2020 Part 6 Table 6.1.5 Type II  - a re-entrant corner exists where both "
          "projections of the structure beyond the corner are greater than 15 percent of "
          "the plan dimension in that direction")
DOMAIN = "structural"
ELEMENT = "IfcBuildingStorey"

MAX_PROJECTION_FRACTION = 0.15
# The corner block cut out, as a fraction of the plan in each direction.
NOTCH_FRACTION = 0.35
GRID_CELLS = 20
MIN_FILLED_FRACTION = 0.35
MIN_ELEMENTS_PER_STOREY = 8
MIN_PLAUSIBLE_EXTENT_MM = 5000.0

_CORNERS = (
    ("south-west", False, False),
    ("south-east", True, False),
    ("north-west", False, True),
    ("north-east", True, True),
)


def _is_load_bearing(wall):
    for pdef in psets_of(wall):
        if pdef.Name == "Pset_WallCommon":
            return prop_value(pdef, "LoadBearing")
    return None


def _lateral_elements(model: ifcopenshell.file):
    walls = sorted_by_guid(model.by_type("IfcWall"))
    flags = [_is_load_bearing(w) for w in walls]
    any_flagged = any(f is True for f in flags)
    return sorted_by_guid(model.by_type("IfcColumn")) + [
        w for w, f in zip(walls, flags) if not any_flagged or f is True
    ]


def _placement_matrix(element):
    import ifcopenshell.util.placement as ifc_placement

    if getattr(element, "ObjectPlacement", None) is None:
        return None
    try:
        return ifc_placement.get_local_placement(element.ObjectPlacement)
    except Exception:
        return None


def _axis_points(element):
    """Local 2D points of the element's Axis polyline, if it has one."""
    rep = getattr(element, "Representation", None)
    if rep is None:
        return []
    for r in rep.Representations:
        if r.RepresentationIdentifier != "Axis":
            continue
        for item in r.Items:
            if item.is_a("IfcPolyline"):
                return [tuple(p.Coordinates[:2]) for p in item.Points]
    return []


def _element_plan_points(element, unit_mm: float, sample_step_mm: float):
    """Where the element sits in plan, in mm.

    A column is one point; a wall is its axis line sampled along its length.
    This has to match how the clause is checked: measuring a wall by its
    placement origin alone leaves the plan raster far too empty to read an
    outline from, and the prediction made here would not match what a
    checker sees.
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
        start, end = to_world(x0, y0), to_world(x1, y1)
        length = math.dist(start, end)
        steps = max(1, int(length / sample_step_mm) + 1) if sample_step_mm > 0 else 1
        for i in range(steps + 1):
            t = i / steps
            points.append((start[0] + (end[0] - start[0]) * t,
                           start[1] + (end[1] - start[1]) * t))
    return points or [to_world(0.0, 0.0)]


def _storey_elements(model: ifcopenshell.file, exclude=frozenset()):
    """storey GlobalId -> [(element, origin x mm, origin y mm)]."""
    scale = length_unit_scale(model)
    out: dict[str, list] = {}
    for element in _lateral_elements(model):
        if element.GlobalId in exclude:
            continue
        storey = storey_of(model, element)
        if storey is None:
            continue
        xyz = global_xyz_mm(model, element, scale)
        if xyz is None:
            continue
        out.setdefault(storey.GlobalId, []).append((element, xyz[0], xyz[1]))
    return out


def _raster(sample_points, x_min, y_min, cell_x, cell_y):
    occupied = set()
    for x, y in sample_points:
        i = min(GRID_CELLS - 1, max(0, int((x - x_min) / cell_x)))
        j = min(GRID_CELLS - 1, max(0, int((y - y_min) / cell_y)))
        occupied.add((i, j))
    return occupied


def _filled(occupied):
    """Row-fill and column-fill intersected  - the storey's plan outline."""
    row_span: dict[int, tuple[int, int]] = {}
    col_span: dict[int, tuple[int, int]] = {}
    for i, j in occupied:
        lo, hi = row_span.get(j, (i, i))
        row_span[j] = (min(lo, i), max(hi, i))
        lo, hi = col_span.get(i, (j, j))
        col_span[i] = (min(lo, j), max(hi, j))
    out = set()
    for j, (i_lo, i_hi) in row_span.items():
        for i in range(i_lo, i_hi + 1):
            span = col_span.get(i)
            if span is not None and span[0] <= j <= span[1]:
                out.add((i, j))
    return out


def _corner_notch(filled, flip_x, flip_y):
    def is_empty(i, j):
        x = (GRID_CELLS - 1 - i) if flip_x else i
        y = (GRID_CELLS - 1 - j) if flip_y else j
        return (x, y) not in filled

    best = (0, 0)
    run = GRID_CELLS
    for i in range(GRID_CELLS):
        depth = 0
        while depth < GRID_CELLS and is_empty(i, depth):
            depth += 1
        run = min(run, depth)
        if run == 0:
            break
        if (i + 1) * run > best[0] * best[1]:
            best = (i + 1, run)
    return best[0] / GRID_CELLS, best[1] / GRID_CELLS


def _analyse(model: ifcopenshell.file, exclude=frozenset()):
    unit_mm = 1.0 / (length_unit_scale(model) * 1000.0) * 1000.0
    by_storey = _storey_elements(model, exclude)
    storeys = {s.GlobalId: s for s in model.by_type("IfcBuildingStorey")}

    out = []
    for storey_gid in sorted(by_storey):
        storey = storeys.get(storey_gid)
        entries = by_storey[storey_gid]
        if storey is None or len(entries) < MIN_ELEMENTS_PER_STOREY:
            continue

        origins_x = [e[1] for e in entries]
        origins_y = [e[2] for e in entries]
        x_min, x_max = min(origins_x), max(origins_x)
        y_min, y_max = min(origins_y), max(origins_y)
        width, depth = x_max - x_min, y_max - y_min
        if width < MIN_PLAUSIBLE_EXTENT_MM or depth < MIN_PLAUSIBLE_EXTENT_MM:
            continue
        cell_x, cell_y = width / GRID_CELLS, depth / GRID_CELLS
        sample_step = max(min(cell_x, cell_y) / 2.0, 1.0)

        # Each element's own plan footprint, so deleting an element removes
        # exactly the cells it occupied.
        samples = {
            element.GlobalId: _element_plan_points(element, unit_mm, sample_step)
            for element, _x, _y in entries
        }
        all_points = [pt for pts in samples.values() for pt in pts]
        if not all_points:
            continue

        before_filled = _filled(_raster(all_points, x_min, y_min, cell_x, cell_y))
        if len(before_filled) < MIN_FILLED_FRACTION * GRID_CELLS * GRID_CELLS:
            continue

        for corner_name, high_x, high_y in _CORNERS:
            fx_before, fy_before = _corner_notch(before_filled, high_x, high_y)
            # Already notched here: the defect would not be an injected one.
            if fx_before > MAX_PROJECTION_FRACTION and fy_before > MAX_PROJECTION_FRACTION:
                continue

            x_cut = (x_max - NOTCH_FRACTION * width) if high_x else (x_min + NOTCH_FRACTION * width)
            y_cut = (y_max - NOTCH_FRACTION * depth) if high_y else (y_min + NOTCH_FRACTION * depth)

            def wholly_inside(points):
                """Every sample point of the element sits in the corner block.

                Requiring ALL of them keeps a wall that merely reaches into
                the corner  - and whose other end holds part of a wing - out
                of the deletion, so the wings stay intact.
                """
                if not points:
                    return False
                for x, y in points:
                    in_x = x >= x_cut if high_x else x <= x_cut
                    in_y = y >= y_cut if high_y else y <= y_cut
                    if not (in_x and in_y):
                        return False
                return True

            removed = [e for e in entries if wholly_inside(samples[e[0].GlobalId])]
            kept = [e for e in entries if not wholly_inside(samples[e[0].GlobalId])]
            if not removed or len(kept) < MIN_ELEMENTS_PER_STOREY:
                continue

            kept_points = [pt for e in kept for pt in samples[e[0].GlobalId]]
            if not kept_points:
                continue
            # The bounding rectangle must survive, or the plan simply got
            # smaller instead of gaining a notch.
            kept_xs = [pt[0] for pt in kept_points]
            kept_ys = [pt[1] for pt in kept_points]
            if (min(kept_xs) > x_min + cell_x or max(kept_xs) < x_max - cell_x
                    or min(kept_ys) > y_min + cell_y or max(kept_ys) < y_max - cell_y):
                continue

            after_filled = _filled(_raster(kept_points, x_min, y_min, cell_x, cell_y))
            if len(after_filled) < MIN_FILLED_FRACTION * GRID_CELLS * GRID_CELLS:
                continue
            fx_after, fy_after = _corner_notch(after_filled, high_x, high_y)
            if fx_after <= MAX_PROJECTION_FRACTION or fy_after <= MAX_PROJECTION_FRACTION:
                continue

            out.append({
                "storey": storey, "corner": corner_name,
                "removed": removed, "kept": kept,
                "width_mm": width, "depth_mm": depth,
                "projection_x": fx_after, "projection_y": fy_after,
            })
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    options = _analyse(model)
    if not options:
        storeys = len(model.by_type("IfcBuildingStorey"))
        return Applicability(
            False,
            f"no storey plan can have a corner removed and still read as an outline with a "
            f"re-entrant corner ({storeys} storey(s))",
        )
    return Applicability(True, f"{len(options)} storey/corner combination(s) can be notched")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    options = _analyse(model, exclude)
    if not options:
        return []

    out = []
    for option in options:
        storey = option["storey"]
        removed = option["removed"]
        out.append(ScoredTarget(
            global_id=storey.GlobalId,  # the STOREY is the target
            # The smallest deletion that still yields the deepest notch.
            score=(option["projection_x"] + option["projection_y"]) * 1000.0 - len(removed),
            justification=(
                f"storey '{storey.Name}' spans {option['width_mm']:.0f}x{option['depth_mm']:.0f}mm; "
                f"deleting {len(removed)} lateral element(s) from its {option['corner']} corner "
                f"leaves projections of {option['projection_x']:.0%} and "
                f"{option['projection_y']:.0%}"
            ),
            element_ids=tuple(p[0].id() for p in removed),
            extra={
                "storey_name": storey.Name,
                "corner": option["corner"],
                "width_mm": option["width_mm"],
                "depth_mm": option["depth_mm"],
                "projection_x": option["projection_x"],
                "projection_y": option["projection_y"],
                "removed_global_ids": [p[0].GlobalId for p in removed],
                "kept_count": len(option["kept"]),
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    deleted = []
    cascade_removed: list[str] = []
    cascade_modified: list[str] = []
    relationship_types: set[str] = set()

    for gid in list(target.extra["removed_global_ids"]):
        element = model.by_guid(gid)
        deleted.append({
            "global_id": gid,
            "type": element.is_a(),
            "name": element.Name,
            "step_id": element.id(),
            "location_mm": global_xyz_mm(model, element),
        })
        cascade = delete_element(model, element)
        cascade_removed.extend(cascade["removed_global_ids"])
        cascade_modified.extend(cascade["modified_global_ids"])
        relationship_types.update(cascade["relationship_types"])

    removed_set = set(cascade_removed)
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcBuildingStorey",
        target_global_id=target.global_id,
        attribute=f"plan outline at the {target.extra['corner']} corner",
        before="rectangular plan",
        after=(
            f"L-shaped plan, projections {target.extra['projection_x']:.0%} x "
            f"{target.extra['projection_y']:.0%}"
        ),
        clause=CLAUSE,
        description=(
            f"Storey '{target.extra['storey_name']}' turned from a "
            f"{target.extra['width_mm']:.0f}x{target.extra['depth_mm']:.0f}mm rectangle into an L "
            f"by deleting {len(deleted)} lateral element(s) from its "
            f"{target.extra['corner']} corner; the projections beyond the resulting re-entrant "
            f"corner are {target.extra['projection_x']:.0%} and "
            f"{target.extra['projection_y']:.0%} of the plan, over the "
            f"{MAX_PROJECTION_FRACTION:.0%} limit"
        ),
        extra={
            "storey_name": target.extra["storey_name"],
            "corner": target.extra["corner"],
            "width_mm": target.extra["width_mm"],
            "depth_mm": target.extra["depth_mm"],
            "projection_x": target.extra["projection_x"],
            "projection_y": target.extra["projection_y"],
            "max_projection_fraction": MAX_PROJECTION_FRACTION,
            "deleted_global_ids": [d["global_id"] for d in deleted],
            "deleted_count": len(deleted),
            "deleted_elements": deleted,
            "kept_count": target.extra["kept_count"],
            "cascade_removed_global_ids": sorted(removed_set),
            "cascade_modified_global_ids": sorted(set(cascade_modified) - removed_set),
            "relationship_types_cleaned": sorted(relationship_types),
            "mechanism": "bulk deletion of the lateral elements inside one plan corner block",
        },
    )
