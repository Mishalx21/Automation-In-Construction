"""
S8  - vertical geometric irregularity: a storey set back far enough from the
one below it to be an irregular structure under BNBC Table 6.1.4 Type III.

Mechanism: delete the lateral elements of one storey that sit beyond a cut
line, so that storey's plan dimension shrinks to less than 1/1.3 of the
storey next to it. What is left is a real setback  - the upper floor
genuinely stops short of the lower one  - rather than a number edited in a
property somewhere.

This is a bulk deletion, like S5, so the mutation record carries every
GlobalId that went and the cut that decided them.

Two things constrain the cut, both taken from how the clause is checked:
the storey has to keep enough lateral elements afterwards to still describe
a plan dimension at all, and the storey it is compared against has to be its
neighbour in elevation. A storey that cannot satisfy both is not offered.
"""
from __future__ import annotations

import statistics

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import delete_element
from .helpers import (
    global_xyz_mm, length_unit_scale, prop_value, psets_of, sorted_by_guid,
    storey_of, storeys_sorted,
)

RULE_ID = "S8"
CLAUSE = ("BNBC 2020 Part 6 Table 6.1.4 Type III  - vertical geometric irregularity exists "
          "where the horizontal dimension of the lateral force-resisting system in any "
          "storey is more than 130 percent of that in an adjacent storey")
DOMAIN = "structural"
ELEMENT = "IfcBuildingStorey"

MAX_RATIO = 1.30
# The cut aims past the limit, so the violation survives the checker's own
# rounding and its slightly different extent measurement.
TARGET_RATIO = 1.60
MIN_ELEMENTS_PER_STOREY = 3
MIN_COMPARABLE_FRACTION = 0.4
MIN_PLAUSIBLE_EXTENT_MM = 1000.0


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


def _storey_points(model: ifcopenshell.file, exclude=frozenset()):
    """storey GlobalId -> [(element, x mm, y mm)], for placed lateral elements."""
    scale = length_unit_scale(model)
    out: dict[str, list] = {}
    for element in _lateral_elements(model):
        if element.GlobalId in exclude:
            continue
        storey = storey_of(model, element)
        if storey is None or storey.Elevation is None:
            continue
        xyz = global_xyz_mm(model, element, scale)
        if xyz is None:
            continue
        out.setdefault(storey.GlobalId, []).append((element, xyz[0], xyz[1]))
    return out


def _plan(points, index):
    values = [p[1 + index] for p in points]
    return min(values), max(values)


def _analyse(model: ifcopenshell.file, exclude=frozenset()):
    """Every storey pair a setback could be cut into, with the cut itself."""
    points = _storey_points(model, exclude)
    ordered = [s for s in storeys_sorted(model)
               if s.Elevation is not None and points.get(s.GlobalId)]
    if len(ordered) < 2:
        return []

    counts = [len(points[s.GlobalId]) for s in ordered]
    median = statistics.median(counts)
    min_comparable = max(MIN_ELEMENTS_PER_STOREY, median * MIN_COMPARABLE_FRACTION)

    out = []
    for lower, upper in zip(ordered, ordered[1:]):
        for target, neighbour in ((upper, lower), (lower, upper)):
            target_points = points[target.GlobalId]
            neighbour_points = points[neighbour.GlobalId]
            if len(target_points) < min_comparable or len(neighbour_points) < min_comparable:
                continue
            for axis, index in (("X", 0), ("Y", 1)):
                t_lo, t_hi = _plan(target_points, index)
                n_lo, n_hi = _plan(neighbour_points, index)
                target_extent = t_hi - t_lo
                neighbour_extent = n_hi - n_lo
                if min(target_extent, neighbour_extent) < MIN_PLAUSIBLE_EXTENT_MM:
                    continue
                # Already irregular on this axis: nothing to inject.
                if max(target_extent, neighbour_extent) / min(target_extent, neighbour_extent) > MAX_RATIO:
                    continue
                wanted = neighbour_extent / TARGET_RATIO
                if wanted >= target_extent:
                    continue
                cut = t_lo + wanted
                kept = [p for p in target_points if p[1 + index] <= cut]
                removed = [p for p in target_points if p[1 + index] > cut]
                if not removed or len(kept) < min_comparable:
                    continue
                kept_lo, kept_hi = _plan(kept, index)
                if kept_hi - kept_lo <= 0:
                    continue
                ratio = neighbour_extent / (kept_hi - kept_lo)
                if ratio <= MAX_RATIO:
                    continue
                out.append({
                    "storey": target,
                    "neighbour": neighbour,
                    "axis": axis,
                    "index": index,
                    "cut_mm": cut,
                    "kept": kept,
                    "removed": removed,
                    "before_extent_mm": target_extent,
                    "after_extent_mm": kept_hi - kept_lo,
                    "neighbour_extent_mm": neighbour_extent,
                    "after_ratio": ratio,
                    "min_comparable": min_comparable,
                })
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    options = _analyse(model)
    if not options:
        storeys = len(model.by_type("IfcBuildingStorey"))
        return Applicability(
            False,
            f"no storey can be cut back far enough to be irregular against its neighbour "
            f"while keeping enough lateral elements to measure ({storeys} storey(s))",
        )
    return Applicability(True, f"{len(options)} storey/axis combination(s) can be set back")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    options = _analyse(model, exclude)
    if not options:
        return []

    out = []
    for option in options:
        storey = option["storey"]
        removed = option["removed"]
        out.append(ScoredTarget(
            global_id=storey.GlobalId,  # the STOREY is the target, not any one element
            # Prefer the cut that removes the fewest elements for the most
            # irregularity: the smallest edit that still makes the point.
            score=option["after_ratio"] * 1000.0 - len(removed),
            justification=(
                f"storey '{storey.Name}' spans {option['before_extent_mm']:.0f}mm on {option['axis']} "
                f"against {option['neighbour_extent_mm']:.0f}mm on '{option['neighbour'].Name}'; "
                f"deleting {len(removed)} lateral element(s) past the cut leaves "
                f"{option['after_extent_mm']:.0f}mm, a ratio of {option['after_ratio']:.2f}"
            ),
            element_ids=tuple(p[0].id() for p in removed),
            extra={
                "storey_name": storey.Name,
                "neighbour_name": option["neighbour"].Name,
                "axis": option["axis"],
                "cut_mm": option["cut_mm"],
                "before_extent_mm": option["before_extent_mm"],
                "after_extent_mm": option["after_extent_mm"],
                "neighbour_extent_mm": option["neighbour_extent_mm"],
                "after_ratio": option["after_ratio"],
                "removed_global_ids": [p[0].GlobalId for p in removed],
                "kept_count": len(option["kept"]),
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    removed_global_ids = list(target.extra["removed_global_ids"])

    deleted = []
    cascade_removed: list[str] = []
    cascade_modified: list[str] = []
    relationship_types: set[str] = set()

    for gid in removed_global_ids:
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
        attribute=f"plan dimension on {target.extra['axis']}",
        before=target.extra["before_extent_mm"],
        after=target.extra["after_extent_mm"],
        clause=CLAUSE,
        description=(
            f"Storey '{target.extra['storey_name']}' set back on the {target.extra['axis']} axis "
            f"from {target.extra['before_extent_mm']:.0f}mm to "
            f"{target.extra['after_extent_mm']:.0f}mm by deleting {len(deleted)} lateral "
            f"element(s) past the cut, against {target.extra['neighbour_extent_mm']:.0f}mm on "
            f"the adjacent storey '{target.extra['neighbour_name']}'  - a ratio of "
            f"{target.extra['after_ratio']:.2f}, over the {MAX_RATIO:.2f} limit"
        ),
        extra={
            "storey_name": target.extra["storey_name"],
            "neighbour_name": target.extra["neighbour_name"],
            "axis": target.extra["axis"],
            "cut_mm": target.extra["cut_mm"],
            "after_ratio": target.extra["after_ratio"],
            "max_ratio": MAX_RATIO,
            "deleted_global_ids": [d["global_id"] for d in deleted],
            "deleted_count": len(deleted),
            "deleted_elements": deleted,
            "kept_count": target.extra["kept_count"],
            "cascade_removed_global_ids": sorted(removed_set),
            "cascade_modified_global_ids": sorted(set(cascade_modified) - removed_set),
            "relationship_types_cleaned": sorted(relationship_types),
            "mechanism": "bulk deletion of the lateral elements past a plan cut line",
        },
    )
