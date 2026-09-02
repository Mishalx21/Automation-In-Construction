"""Geometry perturbation operators (directrix edits, placement edits).

Supported directrix idioms:

* ``IfcIndexedPolyCurve`` (IFC4) — coordinates edited in the
  ``IfcCartesianPointList`` ``CoordList``; hooks are ``IfcArcIndex`` triples.
* ``IfcCompositeCurve`` (IFC2X3/IFC4, the Revit export idiom) — terminal
  straight segments are 2-point ``IfcPolyline`` parents or ``IfcTrimmedCurve``
  parents over ``IfcLine``; endpoints are replaced with NEW
  ``IfcCartesianPoint`` entities (never mutated in place, points may be
  shared).

Known limitations (documented, raise ``UnsupportedGeometryError``):

* ``set_arc_angle`` supports ``IfcIndexedPolyCurve`` arcs only. Changing a
  trimmed-circle arc in a composite curve would break the positional
  continuity of every downstream segment; for composite-curve corpora the
  planner uses ``insert_hooked_bar`` instead.
* ``set_arc_angle`` keeps the arc's chord (endpoints) fixed and recomputes
  the on-arc midpoint, which changes the bend radius and gives up tangent
  continuity with the adjacent straights. Checkers measure angle/tail/radius
  from the directrix, so this is irrelevant for fixtures, but the geometry is
  not shop-drawing realistic.
* ``shorten_hook_tail`` on a composite curve does not update the
  ``IfcSweptDiskSolid`` ``StartParam``/``EndParam`` (Revit writes the curve
  length there); directrix parsers are unaffected.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import ifcopenshell
import ifcopenshell.guid

from bnbc.fixtures import measure
from bnbc.fixtures.errors import TargetNotFoundError, UnsupportedGeometryError
from bnbc.fixtures.operators.base import Operator, OperatorResult, register

_EPS = 1e-9


def _single(targets: list) -> Any:
    if len(targets) != 1:
        raise TargetNotFoundError(f"expected exactly one target, got {len(targets)}")
    return targets[0]


def _directrix(bar):
    solid = measure.find_swept_disk(bar)
    if solid is None:
        raise UnsupportedGeometryError(f"{bar.is_a()} {bar.GlobalId}: no IfcSweptDiskSolid")
    return solid.Directrix


def _pt3(coords):
    c = [float(v) for v in coords]
    while len(c) < 3:
        c.append(0.0)
    return (c[0], c[1], c[2])


def _lerp_from(anchor, p, factor):
    return tuple(anchor[i] + (p[i] - anchor[i]) * factor for i in range(3))


def _pick_hook_end(bar, hook_angle_deg: Optional[float], end: Optional[str]) -> str:
    if end in ("start", "end"):
        return end
    candidates = []
    for e in ("end", "start"):
        hook = measure.terminal_hook(bar, end=e)
        if hook is None:
            continue
        if hook_angle_deg is None or abs(hook.angle_deg - hook_angle_deg) <= 15.0:
            candidates.append(e)
    if not candidates:
        raise TargetNotFoundError(
            f"bar {bar.GlobalId}: no terminal hook"
            + (f" with angle ~{hook_angle_deg} deg" if hook_angle_deg is not None else "")
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# shorten_hook_tail
# ---------------------------------------------------------------------------

@register
class ShortenHookTail(Operator):
    """Set the terminal straight tail of a hooked bar to ``new_tail_mm``.

    The tail run (all consecutive straight segments at the chosen end, up to
    the hook arc) is scaled uniformly about its junction with the arc, so the
    measured tail length equals ``new_tail_mm`` exactly regardless of how
    many sub-segments compose it. Works for lengthening too, despite the
    name.
    """

    name = "shorten_hook_tail"

    def _apply(self, model, target_selector, *, new_tail_mm: float,
               hook_angle_deg: Optional[float] = None, end: Optional[str] = None,
               **_ignored) -> OperatorResult:
        bar = _single(self.resolve_targets(model, target_selector, "IfcReinforcingBar"))
        chosen_end = _pick_hook_end(bar, hook_angle_deg, end)
        hook = measure.terminal_hook(bar, end=chosen_end)
        if hook is None or hook.tail_mm <= _EPS:
            raise UnsupportedGeometryError(f"bar {bar.GlobalId}: no measurable tail at {chosen_end}")
        scale = measure.unit_scale_mm(model)
        factor = float(new_tail_mm) / hook.tail_mm

        directrix = _directrix(bar)
        if directrix.is_a("IfcIndexedPolyCurve"):
            _scale_tail_indexed(directrix, chosen_end, factor)
        elif directrix.is_a("IfcCompositeCurve"):
            _scale_tail_composite(model, directrix, chosen_end, factor, scale)
        else:
            raise UnsupportedGeometryError(
                f"bar {bar.GlobalId}: unsupported directrix {directrix.is_a()}"
            )

        claim = (
            f"tail of {hook.angle_deg:.0f}-deg hook on bar {bar.GlobalId} ({chosen_end}) "
            f"set from {hook.tail_mm:.1f} mm to {float(new_tail_mm):.1f} mm"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={"new_tail_mm": float(new_tail_mm), "hook_angle_deg": hook_angle_deg,
                    "end": chosen_end},
            affected_guids=[bar.GlobalId], claim=claim,
            expected=[{
                "check": "terminal_tail", "guid": bar.GlobalId, "end": chosen_end,
                "value_mm": float(new_tail_mm), "tol_mm": 0.5,
            }],
        )


def _terminal_line_run_indexed(directrix, end: str):
    """(anchor_coord_index, moving_coord_indices) of the terminal straight run."""
    seg_list = list(getattr(directrix, "Segments", None) or [])
    coord_list = list(directrix.Points.CoordList)
    if not seg_list:
        if len(coord_list) < 2:
            raise UnsupportedGeometryError("indexed polycurve with <2 points")
        if end == "end":
            return len(coord_list) - 2, [len(coord_list) - 1]
        return 1, [0]

    ordered = list(reversed(seg_list)) if end == "end" else seg_list
    path: list[int] = []  # 0-based coord indices along the run, terminal-first
    for seg in ordered:
        idx = measure.unwrap(seg)
        indices = [int(i) - 1 for i in idx]
        is_arc = seg.is_a("IfcArcIndex") if hasattr(seg, "is_a") else len(indices) == 3
        if is_arc:
            break
        if end == "end":
            indices = list(reversed(indices))
        for i in indices:
            if not path or path[-1] != i:
                path.append(i)
    if len(path) < 2:
        raise UnsupportedGeometryError(f"no terminal straight run at {end}")
    anchor = path[-1]          # junction with the arc
    moving = path[:-1]         # everything terminal-side of the anchor
    return anchor, moving


def _scale_tail_indexed(directrix, end: str, factor: float) -> None:
    anchor_idx, moving = _terminal_line_run_indexed(directrix, end)
    points_obj = directrix.Points
    coords = [tuple(float(v) for v in c) for c in points_obj.CoordList]
    dim = len(coords[0])
    anchor = _pt3(coords[anchor_idx])
    for i in moving:
        new_p = _lerp_from(anchor, _pt3(coords[i]), factor)
        coords[i] = tuple(new_p[:dim])
    points_obj.CoordList = coords


def _composite_terminal_run(directrix, end: str):
    """Composite segments of the terminal straight run (terminal-first order)."""
    segments = list(getattr(directrix, "Segments", None) or [])
    ordered = list(reversed(segments)) if end == "end" else segments
    run = []
    for comp_seg in ordered:
        parent = comp_seg.ParentCurve
        if parent.is_a("IfcPolyline"):
            run.append(comp_seg)
        elif parent.is_a("IfcTrimmedCurve") and parent.BasisCurve.is_a("IfcLine"):
            run.append(comp_seg)
        else:
            break
    if not run:
        raise UnsupportedGeometryError(f"no terminal straight run at {end}")
    return run


def _composite_anchor(run, end: str, scale: float):
    """Model-unit anchor point: the run's junction with the arc."""
    last = run[-1]  # segment adjacent to the arc (run is terminal-first)
    parent = last.ParentCurve
    same_sense = getattr(last, "SameSense", True)
    if parent.is_a("IfcPolyline"):
        pts = [_pt3(p.Coordinates) for p in parent.Points]
    else:
        resolved = measure._trimmed_line_points(parent)
        if resolved is None:
            raise UnsupportedGeometryError("unresolvable trimmed-line trims")
        pts = list(resolved)
    if not same_sense:
        pts = list(reversed(pts))
    # Curve direction: for end='end' the run's last segment starts at the arc,
    # i.e. its curve-order FIRST point is the anchor; for end='start' the
    # segment's curve-order LAST point touches the arc.
    return pts[0] if end == "end" else pts[-1]


def _scale_tail_composite(model, directrix, end: str, factor: float, scale: float) -> None:
    run = _composite_terminal_run(directrix, end)
    anchor = _composite_anchor(run, end, scale)

    def _new_point(p):
        moved = _lerp_from(anchor, _pt3(p), factor)
        return model.create_entity("IfcCartesianPoint", Coordinates=moved)

    for comp_seg in run:
        parent = comp_seg.ParentCurve
        if parent.is_a("IfcPolyline"):
            parent.Points = [ _new_point(_pt3(p.Coordinates)) for p in parent.Points ]
        else:  # IfcTrimmedCurve over IfcLine
            line = parent.BasisCurve
            pnt = _pt3(line.Pnt.Coordinates)
            direction = _pt3(line.Dir.Orientation.DirectionRatios)
            mag = float(measure.unwrap(getattr(line.Dir, "Magnitude", 1.0)))
            nrm = math.sqrt(sum(d * d for d in direction)) or 1.0
            unit_dir = tuple(d / nrm for d in direction)

            def _rewrite(trim_set):
                new_trim = []
                for val in trim_set or ():
                    coords = getattr(val, "Coordinates", None)
                    if coords is not None:
                        new_trim.append(_new_point(_pt3(coords)))
                        continue
                    raw = measure.unwrap(val)
                    if isinstance(raw, (int, float)):
                        # param t maps to point pnt + t*mag*unit_dir; scaling
                        # about the anchor is affine in t iff anchor is on
                        # this basis line — verify before rewriting.
                        p_old = tuple(pnt[i] + float(raw) * mag * unit_dir[i] for i in range(3))
                        p_new = _lerp_from(anchor, p_old, factor)
                        delta = tuple(p_new[i] - pnt[i] for i in range(3))
                        t_new = sum(delta[i] * unit_dir[i] for i in range(3)) / mag
                        off = math.sqrt(max(0.0, sum(d * d for d in delta)
                                            - (t_new * mag) ** 2))
                        if off > 1e-6 * max(1.0, abs(t_new * mag)):
                            raise UnsupportedGeometryError(
                                "parameter-trimmed tail not collinear with anchor"
                            )
                        new_trim.append(model.create_entity("IfcParameterValue", t_new))
                    else:
                        new_trim.append(val)
                return new_trim

            parent.Trim1 = _rewrite(parent.Trim1)
            parent.Trim2 = _rewrite(parent.Trim2)


def shorten_hook_tail(model, bar_guid: str, new_tail_mm: float,
                      hook_angle_deg: Optional[float] = None,
                      end: Optional[str] = None) -> OperatorResult:
    """Convenience wrapper; see :class:`ShortenHookTail` (input model is not mutated)."""
    return ShortenHookTail().apply(model, bar_guid, new_tail_mm=new_tail_mm,
                                   hook_angle_deg=hook_angle_deg, end=end)


# ---------------------------------------------------------------------------
# set_arc_angle
# ---------------------------------------------------------------------------

@register
class SetArcAngle(Operator):
    """Set an IfcArcIndex arc's included angle by recomputing its midpoint.

    Endpoints (the chord) stay fixed; the on-arc midpoint moves along the
    chord's perpendicular bisector so the new circumcircle subtends
    ``target_angle_deg``: R = chord / (2 sin(theta/2)), sagitta
    s = R (1 - cos(theta/2)). Valid for theta in (0, 360) including reflex.
    IfcIndexedPolyCurve only (see module docstring).
    """

    name = "set_arc_angle"

    def _apply(self, model, target_selector, *, target_angle_deg: float,
               arc: Any = "auto", **_ignored) -> OperatorResult:
        bar = _single(self.resolve_targets(model, target_selector, "IfcReinforcingBar"))
        directrix = _directrix(bar)
        if not directrix.is_a("IfcIndexedPolyCurve"):
            raise UnsupportedGeometryError(
                f"set_arc_angle supports IfcIndexedPolyCurve only, got {directrix.is_a()} "
                "(use insert_hooked_bar for composite-curve corpora)"
            )
        seg_list = list(getattr(directrix, "Segments", None) or [])
        arc_positions = [
            k for k, seg in enumerate(seg_list)
            if (seg.is_a("IfcArcIndex") if hasattr(seg, "is_a")
                else len(list(measure.unwrap(seg))) == 3)
        ]
        if not arc_positions:
            raise TargetNotFoundError(f"bar {bar.GlobalId}: directrix has no arcs")
        if arc == "auto":
            ordinal = len(arc_positions) - 1 if len(arc_positions) > 1 else 0
        elif arc == "start":
            ordinal = 0
        elif arc == "end":
            ordinal = len(arc_positions) - 1
        else:
            ordinal = int(arc)
        seg = seg_list[arc_positions[ordinal]]
        indices = [int(i) - 1 for i in measure.unwrap(seg)]
        i0, i1, i2 = indices

        # Reject midpoints reused elsewhere in the curve.
        uses = 0
        for other in seg_list:
            uses += sum(1 for i in measure.unwrap(other) if int(i) - 1 == i1)
        if uses > 1:
            raise UnsupportedGeometryError("arc midpoint index reused by another segment")

        points_obj = directrix.Points
        coords = [tuple(float(v) for v in c) for c in points_obj.CoordList]
        dim = len(coords[0])
        a, b, c = _pt3(coords[i0]), _pt3(coords[i1]), _pt3(coords[i2])
        old_angle, _, _, _ = measure.arc_from_three_points(a, b, c)

        chord_mid = tuple((a[i] + c[i]) / 2.0 for i in range(3))
        chord = math.dist(a, c)
        d = tuple(b[i] - chord_mid[i] for i in range(3))
        d_len = math.sqrt(sum(v * v for v in d))
        if chord < _EPS or d_len < _EPS:
            raise UnsupportedGeometryError("degenerate arc (zero chord or flat midpoint)")
        d_hat = tuple(v / d_len for v in d)
        theta = math.radians(float(target_angle_deg))
        radius = chord / (2.0 * math.sin(theta / 2.0))
        sagitta = radius * (1.0 - math.cos(theta / 2.0))
        new_mid = tuple(chord_mid[i] + d_hat[i] * sagitta for i in range(3))
        coords[i1] = tuple(new_mid[:dim])
        points_obj.CoordList = coords

        claim = (
            f"arc #{ordinal} of bar {bar.GlobalId} set from {old_angle:.1f} deg "
            f"to {float(target_angle_deg):.1f} deg"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={"target_angle_deg": float(target_angle_deg), "arc": arc},
            affected_guids=[bar.GlobalId], claim=claim,
            expected=[{
                "check": "arc_angle", "guid": bar.GlobalId, "arc_ordinal": ordinal,
                "value_deg": float(target_angle_deg), "tol_deg": 1.0,
            }],
        )


def set_arc_angle(model, bar_guid: str, target_angle_deg: float,
                  arc: Any = "auto") -> OperatorResult:
    """Convenience wrapper; see :class:`SetArcAngle` (input model is not mutated)."""
    return SetArcAngle().apply(model, bar_guid, target_angle_deg=target_angle_deg, arc=arc)


# ---------------------------------------------------------------------------
# translate_elements / copy_element_offset
# ---------------------------------------------------------------------------

def _world_matrix(element, model=None):
    """World placement matrix of ``element``.

    Elements without an ObjectPlacement (inserted synthetic bars carry none)
    get an identity placement at the origin when ``model`` is given —
    operators must be closed under composition: an insert_* output is a valid
    input to every placement-based operator (observed live: a spec-authored
    insert→copy chain crashed the build on 8.3.4.2).
    """
    import ifcopenshell.util.placement
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        if model is None:
            raise UnsupportedGeometryError(f"{element.is_a()} has no ObjectPlacement")
        axis = model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)
            ),
        )
        placement = model.create_entity("IfcLocalPlacement", RelativePlacement=axis)
        element.ObjectPlacement = placement
    return ifcopenshell.util.placement.get_local_placement(placement)


def _make_world_placement(model, matrix, delta_units=(0.0, 0.0, 0.0)):
    """New absolute IfcLocalPlacement realising ``matrix`` shifted by delta."""
    origin = (
        float(matrix[0][3]) + delta_units[0],
        float(matrix[1][3]) + delta_units[1],
        float(matrix[2][3]) + delta_units[2],
    )
    z_axis = (float(matrix[0][2]), float(matrix[1][2]), float(matrix[2][2]))
    x_axis = (float(matrix[0][0]), float(matrix[1][0]), float(matrix[2][0]))
    axis2 = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=origin),
        Axis=model.create_entity("IfcDirection", DirectionRatios=z_axis),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=x_axis),
    )
    return model.create_entity("IfcLocalPlacement", PlacementRelTo=None,
                               RelativePlacement=axis2), origin


@register
class TranslateElements(Operator):
    """Rigid world translation of elements by (dx, dy, dz) millimetres.

    Each element's composed world matrix is recomputed, shifted, and rebuilt
    as a NEW absolute ``IfcLocalPlacement`` (PlacementRelTo = world), so the
    orientation is preserved exactly and other elements chained off the old
    placement are unaffected.
    """

    name = "translate_elements"

    def _apply(self, model, target_selector, *, dx_mm: float = 0.0, dy_mm: float = 0.0,
               dz_mm: float = 0.0, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector)
        scale = measure.unit_scale_mm(model)
        delta_units = (dx_mm / scale, dy_mm / scale, dz_mm / scale)
        expected = []
        guids = []
        for el in targets:
            matrix = _world_matrix(el, model)
            placement, origin = _make_world_placement(model, matrix, delta_units)
            el.ObjectPlacement = placement
            guids.append(el.GlobalId)
            expected.append({
                "check": "placement_origin", "guid": el.GlobalId,
                "value_mm": [o * scale for o in origin], "tol_mm": 0.1,
            })
        claim = (
            f"translated {len(guids)} element(s) by ({dx_mm:.1f}, {dy_mm:.1f}, {dz_mm:.1f}) mm"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={"dx_mm": dx_mm, "dy_mm": dy_mm, "dz_mm": dz_mm},
            affected_guids=guids, claim=claim, expected=expected,
        )


def translate_elements(model, guids, dx_mm=0.0, dy_mm=0.0, dz_mm=0.0) -> OperatorResult:
    """Convenience wrapper; see :class:`TranslateElements` (input model is not mutated)."""
    return TranslateElements().apply(model, guids, dx_mm=dx_mm, dy_mm=dy_mm, dz_mm=dz_mm)


@register
class CopyElementOffset(Operator):
    """Duplicate an element at a world offset (spacing-violation factory).

    Minimal deep copy: attributes are copied entity-shallow (the geometric
    ``Representation`` graph is SHARED with the original — legal in IFC and
    exactly what a spacing fixture needs), with a fresh GlobalId and a new
    absolute placement at ``offset_mm`` from the original's world position.
    The copy is appended to the original's spatial containment AND its host
    aggregation/nesting relationships (a copied bar lives in the same beam as
    its original — required by common-host layer/spacing conventions);
    pset/material relationship inverses are NOT duplicated.
    """

    name = "copy_element_offset"

    def _apply(self, model, target_selector, *, offset_mm=(0.0, 0.0, 0.0),
               new_name: Optional[str] = None, **_ignored) -> OperatorResult:
        el = _single(self.resolve_targets(model, target_selector))
        scale = measure.unit_scale_mm(model)
        delta_units = tuple(float(o) / scale for o in offset_mm)
        matrix = _world_matrix(el, model)
        placement, origin = _make_world_placement(model, matrix, delta_units)

        info = el.get_info()
        info.pop("id", None)
        info.pop("type", None)
        info["GlobalId"] = ifcopenshell.guid.new()
        info["ObjectPlacement"] = placement
        if new_name is not None:
            info["Name"] = new_name
        elif info.get("Name"):
            info["Name"] = f"{info['Name']} (fixture copy)"
        new_el = model.create_entity(el.is_a(), **info)

        for rel in getattr(el, "ContainedInStructure", None) or []:
            rel.RelatedElements = list(rel.RelatedElements) + [new_el]
        # The copy belongs to the SAME host as the original: without the
        # aggregation/nesting relationships a common-host layer convention
        # never sees the pair (observed live: every 8.1.6.1 spacing fixture
        # read as "no_layer" because the copied bar was host-less).
        for inv in ("Decomposes", "Nests"):
            for rel in getattr(el, inv, None) or []:
                rel.RelatedObjects = list(rel.RelatedObjects) + [new_el]

        claim = (
            f"copied {el.is_a()} {el.GlobalId} -> {new_el.GlobalId} at offset "
            f"({offset_mm[0]:.1f}, {offset_mm[1]:.1f}, {offset_mm[2]:.1f}) mm"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={"offset_mm": list(offset_mm), "new_name": new_name},
            affected_guids=[el.GlobalId, new_el.GlobalId], claim=claim,
            expected=[
                {"check": "guid_present", "guid": new_el.GlobalId,
                 "ifc_class": el.is_a()},
                {"check": "placement_origin", "guid": new_el.GlobalId,
                 "value_mm": [o * scale for o in origin], "tol_mm": 0.1},
            ],
        )


def copy_element_offset(model, guid: str, offset_mm=(0.0, 0.0, 0.0),
                        new_name: Optional[str] = None) -> OperatorResult:
    """Convenience wrapper; see :class:`CopyElementOffset` (input model is not mutated)."""
    return CopyElementOffset().apply(model, guid, offset_mm=offset_mm, new_name=new_name)


# ---------------------------------------------------------------------------
# resize_profile
# ---------------------------------------------------------------------------

@register
class ResizeProfile(Operator):
    """Set the rectangle-profile dimensions of an extruded member (mm).

    The target's ``IfcExtrudedAreaSolid`` gets a NEW ``IfcRectangleProfileDef``
    (profiles may be shared between members; the original is never mutated).
    Omitted dims keep their current value. Dimensional-violation factory for
    columns/beams.
    """

    name = "resize_profile"

    def _apply(self, model, target_selector, *, x_mm: Optional[float] = None,
               y_mm: Optional[float] = None, **_ignored) -> OperatorResult:
        el = _single(self.resolve_targets(model, target_selector))
        if x_mm is None and y_mm is None:
            raise UnsupportedGeometryError("resize_profile needs x_mm and/or y_mm")
        scale = measure.unit_scale_mm(model)

        extrusion = None
        rep = getattr(el, "Representation", None)
        for shape_rep in getattr(rep, "Representations", None) or []:
            for item in getattr(shape_rep, "Items", None) or []:
                if item.is_a("IfcExtrudedAreaSolid") and item.SweptArea.is_a("IfcRectangleProfileDef"):
                    extrusion = item
                    break
            if extrusion is not None:
                break
        if extrusion is None:
            raise UnsupportedGeometryError(
                f"{el.is_a()} {el.GlobalId}: no IfcExtrudedAreaSolid over IfcRectangleProfileDef"
            )

        old = extrusion.SweptArea
        new_x = float(x_mm) / scale if x_mm is not None else float(measure.unwrap(old.XDim))
        new_y = float(y_mm) / scale if y_mm is not None else float(measure.unwrap(old.YDim))
        extrusion.SweptArea = model.create_entity(
            "IfcRectangleProfileDef",
            ProfileType=old.ProfileType,
            ProfileName=getattr(old, "ProfileName", None),
            Position=getattr(old, "Position", None),
            XDim=new_x,
            YDim=new_y,
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={"x_mm": x_mm, "y_mm": y_mm},
            affected_guids=[el.GlobalId],
            claim=f"profile of {el.is_a()} {el.GlobalId} set to "
                  f"{new_x * scale:.1f} x {new_y * scale:.1f} mm",
            expected=[{
                "check": "profile_dims", "guid": el.GlobalId,
                "x_mm": new_x * scale, "y_mm": new_y * scale, "tol_mm": 0.1,
            }],
        )
