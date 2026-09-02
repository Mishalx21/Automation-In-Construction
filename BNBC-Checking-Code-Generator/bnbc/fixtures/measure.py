"""Independent IFC measurement utilities for fixture self-verification.

DESIGN REQUIREMENT (contracts.SelfVerification): this module must NOT import
``ifc_helpers``. It is a deliberately separate, minimal oracle so that a bug
in the shared helper library cannot silently agree with a bug in a fixture.
Only the Python standard library, numpy, and ifcopenshell itself are used.

All returned lengths are in **millimetres** and all angles in **degrees**
unless a function name says otherwise. Arc angles are the TRUE swept
(included) angle computed either from trim parameters (IfcTrimmedCurve over
IfcCircle) or from the circumcircle of the three IfcArcIndex points — the
naive "2 x inscribed angle at the midpoint" formula is wrong for arcs over
90 degrees (reflex fold); we avoid it entirely by computing the signed sweep
about the circle centre through the on-arc midpoint.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "unit_scale_mm",
    "plane_angle_scale_deg",
    "unwrap",
    "Segment",
    "Hook",
    "arc_from_three_points",
    "find_swept_disk",
    "directrix_segments",
    "terminal_hook",
    "bar_diameter_mm",
    "element_storey_name",
    "find_storey",
    "placement_matrix",
    "placement_origin_mm",
    "directrix_points_world_mm",
    "element_bbox_mm",
    "pset_names",
    "element_by_guid",
    "element_material_names",
    "profile_dims_mm",
]

_SI_PREFIX_FACTOR = {
    "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
    "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2,
    "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
}

_EPS = 1e-9


def unwrap(value: Any) -> Any:
    """Unwrap an IFC wrapped value (IfcReal, IfcParameterValue, ...)."""
    return getattr(value, "wrappedValue", value)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def _project_units(model) -> list:
    projects = model.by_type("IfcProject")
    if not projects:
        return []
    ctx = getattr(projects[0], "UnitsInContext", None)
    if ctx is None:
        return []
    return list(getattr(ctx, "Units", None) or [])


def unit_scale_mm(model) -> float:
    """Millimetres per model length unit (multiply model lengths by this)."""
    for unit in _project_units(model):
        if getattr(unit, "UnitType", None) != "LENGTHUNIT":
            continue
        if unit.is_a("IfcSIUnit"):
            name = (getattr(unit, "Name", "") or "").upper()
            if name not in ("METRE", "METER"):
                continue
            prefix = getattr(unit, "Prefix", None)
            if prefix is None:
                return 1000.0
            return _SI_PREFIX_FACTOR.get(str(prefix).upper(), 1.0) * 1000.0
        if unit.is_a("IfcConversionBasedUnit"):
            cf = getattr(unit, "ConversionFactor", None)
            if cf is not None and getattr(cf, "ValueComponent", None) is not None:
                metres_per_unit = float(unwrap(cf.ValueComponent))
                return metres_per_unit * 1000.0
            uname = (getattr(unit, "Name", "") or "").upper()
            if "INCH" in uname:
                return 25.4
            if "FOOT" in uname or "FEET" in uname:
                return 304.8
    return 1000.0  # assume metres when unspecified


def plane_angle_scale_deg(model) -> float:
    """Degrees per model plane-angle unit (multiply raw angle params by this)."""
    for unit in _project_units(model):
        if getattr(unit, "UnitType", None) != "PLANEANGLEUNIT":
            continue
        if unit.is_a("IfcSIUnit"):
            name = (getattr(unit, "Name", "") or "").upper()
            if name == "RADIAN":
                return 180.0 / math.pi
        if unit.is_a("IfcConversionBasedUnit"):
            uname = (getattr(unit, "Name", "") or "").upper()
            if "DEGREE" in uname or "DEG" in uname:
                return 1.0
            cf = getattr(unit, "ConversionFactor", None)
            if cf is not None and getattr(cf, "ValueComponent", None) is not None:
                rad_per_unit = float(unwrap(cf.ValueComponent))
                return math.degrees(rad_per_unit)
    return 180.0 / math.pi  # IFC default plane-angle unit is radian


# ---------------------------------------------------------------------------
# Small vector helpers (pure Python; keep this module dependency-light)
# ---------------------------------------------------------------------------

def _pt3(coords) -> tuple[float, float, float]:
    c = [float(v) for v in coords]
    while len(c) < 3:
        c.append(0.0)
    return (c[0], c[1], c[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    if n < _EPS:
        return (0.0, 0.0, 0.0)
    return _scale(a, 1.0 / n)


def _dist(a, b):
    return _norm(_sub(a, b))


# ---------------------------------------------------------------------------
# Arc math
# ---------------------------------------------------------------------------

def arc_from_three_points(p_start, p_mid, p_end):
    """True swept angle of the arc start->mid->end.

    Returns ``(angle_deg, radius, arc_length, center)`` in the units of the
    input points. ``angle_deg`` is the TRUE included/swept angle in (0, 360):
    the sweep from start to end measured in the direction that passes through
    the on-arc midpoint. This is correct for reflex arcs too, unlike the
    inscribed-angle shortcut. Collinear/degenerate input returns
    ``(0.0, 0.0, chord, None)``.
    """
    a = _pt3(p_start)
    b = _pt3(p_mid)
    c = _pt3(p_end)

    u = _sub(b, a)
    v = _sub(c, a)
    n = _cross(u, v)
    n2 = _dot(n, n)
    chord = _dist(a, c)
    if n2 < _EPS * max(1.0, _dot(u, u)) * max(1.0, _dot(v, v)):
        return (0.0, 0.0, chord, None)

    # Circumcenter: A + [ (|u|^2 v - |v|^2 u) x n ] / (2 |n|^2)
    w = _sub(_scale(v, _dot(u, u)), _scale(u, _dot(v, v)))
    center = _add(a, _scale(_cross(w, n), 1.0 / (2.0 * n2)))
    radius = _dist(a, center)
    if radius < _EPS:
        return (0.0, 0.0, chord, None)

    # Local frame in the arc plane: x_hat toward start, z_hat = plane normal.
    x_hat = _unit(_sub(a, center))
    z_hat = _unit(n)
    y_hat = _cross(z_hat, x_hat)

    def _theta(p):
        d = _sub(p, center)
        return math.atan2(_dot(d, y_hat), _dot(d, x_hat)) % (2.0 * math.pi)

    theta_mid = _theta(b)
    theta_end = _theta(c)
    # CCW sweep from start (theta=0) to end is theta_end; the arc passes
    # through mid iff theta_mid <= theta_end. Otherwise the true arc runs CW.
    if theta_mid <= theta_end + _EPS:
        sweep = theta_end
    else:
        sweep = 2.0 * math.pi - theta_end
    angle_deg = math.degrees(sweep)
    return (angle_deg, radius, radius * sweep, center)


# ---------------------------------------------------------------------------
# Directrix extraction
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One directrix segment, in millimetres/degrees (local bar coordinates)."""

    kind: str                      # "straight" | "arc"
    length_mm: float
    start_mm: tuple[float, float, float]
    end_mm: tuple[float, float, float]
    angle_deg: float = 0.0         # arcs only: true swept angle
    radius_mm: float = 0.0         # arcs only


@dataclass
class Hook:
    """Terminal hook description: bend angle + straight tail beyond the bend."""

    angle_deg: float
    tail_mm: float
    radius_mm: float
    end: str  # "start" | "end"


def find_swept_disk(bar) -> Optional[Any]:
    """Return the bar's IfcSweptDiskSolid representation item, if any."""
    rep = getattr(bar, "Representation", None)
    if not rep:
        return None
    for shape_rep in getattr(rep, "Representations", None) or []:
        for item in getattr(shape_rep, "Items", None) or []:
            if item.is_a("IfcSweptDiskSolid"):
                return item
    return None


def _axis2_frame(position):
    """Return (origin, x_hat, y_hat, z_hat) of an IfcAxis2Placement2D/3D."""
    origin = _pt3(getattr(position.Location, "Coordinates", (0.0, 0.0, 0.0)))
    if position.is_a("IfcAxis2Placement3D"):
        axis = getattr(position, "Axis", None)
        z_hat = _unit(_pt3(axis.DirectionRatios)) if axis is not None else (0.0, 0.0, 1.0)
        if _norm(z_hat) < _EPS:
            z_hat = (0.0, 0.0, 1.0)
    else:
        z_hat = (0.0, 0.0, 1.0)
    ref = getattr(position, "RefDirection", None)
    rd = _pt3(ref.DirectionRatios) if ref is not None else (1.0, 0.0, 0.0)
    # Gram-Schmidt x against z
    x_hat = _unit(_sub(rd, _scale(z_hat, _dot(rd, z_hat))))
    if _norm(x_hat) < _EPS:
        x_hat = (1.0, 0.0, 0.0)
    y_hat = _cross(z_hat, x_hat)
    return origin, x_hat, y_hat, z_hat


def _trim_angle_rad(trim_set, circle_frame, angle_scale_deg) -> Optional[float]:
    """Angle (radians, in the circle's local frame) from an IfcTrimmingSelect set."""
    origin, x_hat, y_hat, _ = circle_frame
    # Prefer parameter values.
    for val in trim_set or ():
        raw = unwrap(val)
        if isinstance(raw, (int, float)):
            return math.radians(float(raw) * angle_scale_deg)
    # Fall back to cartesian trim points.
    for val in trim_set or ():
        coords = getattr(val, "Coordinates", None)
        if coords is not None:
            d = _sub(_pt3(coords), origin)
            return math.atan2(_dot(d, y_hat), _dot(d, x_hat))
    return None


def _trimmed_circle_segment(parent, scale, angle_scale_deg) -> Optional[Segment]:
    basis = parent.BasisCurve
    radius = float(unwrap(getattr(basis, "Radius", 0.0)))
    frame = _axis2_frame(basis.Position)
    t1 = _trim_angle_rad(getattr(parent, "Trim1", None), frame, angle_scale_deg)
    t2 = _trim_angle_rad(getattr(parent, "Trim2", None), frame, angle_scale_deg)
    if t1 is None or t2 is None or radius <= 0.0:
        return None
    sense = getattr(parent, "SenseAgreement", True)
    if sense:
        sweep = (t2 - t1) % (2.0 * math.pi)
    else:
        sweep = (t1 - t2) % (2.0 * math.pi)
    if sweep < _EPS:
        sweep = 2.0 * math.pi  # full circle trim degenerates; treat as full
    origin, x_hat, y_hat, _ = frame

    def _point_at(theta):
        return _add(
            origin,
            _add(_scale(x_hat, radius * math.cos(theta)), _scale(y_hat, radius * math.sin(theta))),
        )

    p_start = _point_at(t1)
    p_end = _point_at(t2)
    return Segment(
        kind="arc",
        length_mm=radius * sweep * scale,
        start_mm=_scale(p_start, scale),
        end_mm=_scale(p_end, scale),
        angle_deg=math.degrees(sweep),
        radius_mm=radius * scale,
    )


def directrix_segments(bar, unit_scale: Optional[float] = None) -> list[Segment]:
    """Ordered directrix segments of a swept-disk bar, in mm/degrees.

    Supports:
    * ``IfcIndexedPolyCurve`` (IFC4): explicit ``IfcLineIndex``/``IfcArcIndex``
      segments, or the bare point list when ``Segments`` is absent.
    * ``IfcCompositeCurve`` (IFC2X3/IFC4): ``IfcPolyline`` segments and
      ``IfcTrimmedCurve`` segments over ``IfcCircle`` (parameter or cartesian
      trims, honouring ``SenseAgreement`` and the model plane-angle unit) or
      over ``IfcLine`` (parameter or cartesian trims).
    * bare ``IfcPolyline`` directrices.

    Returns ``[]`` when the bar has no parseable swept-disk directrix.
    """
    solid = find_swept_disk(bar)
    if solid is None:
        return []
    directrix = getattr(solid, "Directrix", None)
    if directrix is None:
        return []
    model = bar.file
    scale = unit_scale if unit_scale is not None else unit_scale_mm(model)
    angle_scale_deg = plane_angle_scale_deg(model)
    segments: list[Segment] = []

    def _straight(p1, p2):
        a, b = _pt3(p1), _pt3(p2)
        return Segment(
            kind="straight",
            length_mm=_dist(a, b) * scale,
            start_mm=_scale(a, scale),
            end_mm=_scale(b, scale),
        )

    if directrix.is_a("IfcIndexedPolyCurve"):
        points_obj = getattr(directrix, "Points", None)
        coord_list = list(getattr(points_obj, "CoordList", None) or [])
        if not coord_list:
            return []
        seg_indices = getattr(directrix, "Segments", None) or []
        if not seg_indices:
            for i in range(len(coord_list) - 1):
                segments.append(_straight(coord_list[i], coord_list[i + 1]))
            return segments
        for seg in seg_indices:
            idx = unwrap(seg)
            if not hasattr(idx, "__len__"):
                continue
            indices = [int(i) - 1 for i in idx]  # 1-based -> 0-based
            if any(i < 0 or i >= len(coord_list) for i in indices):
                continue
            is_arc = seg.is_a("IfcArcIndex") if hasattr(seg, "is_a") else len(indices) == 3
            if is_arc and len(indices) == 3:
                p_s, p_m, p_e = (coord_list[i] for i in indices)
                angle_deg, radius, arc_len, _ = arc_from_three_points(p_s, p_m, p_e)
                if angle_deg <= 0.0:
                    segments.append(_straight(p_s, p_e))
                else:
                    segments.append(Segment(
                        kind="arc",
                        length_mm=arc_len * scale,
                        start_mm=_scale(_pt3(p_s), scale),
                        end_mm=_scale(_pt3(p_e), scale),
                        angle_deg=angle_deg,
                        radius_mm=radius * scale,
                    ))
            else:
                for k in range(len(indices) - 1):
                    segments.append(_straight(coord_list[indices[k]], coord_list[indices[k + 1]]))
        return segments

    if directrix.is_a("IfcCompositeCurve"):
        for comp_seg in getattr(directrix, "Segments", None) or []:
            parent = getattr(comp_seg, "ParentCurve", None)
            if parent is None:
                continue
            same_sense = getattr(comp_seg, "SameSense", True)
            new_segments: list[Segment] = []
            if parent.is_a("IfcPolyline"):
                pts = [_pt3(p.Coordinates) for p in (getattr(parent, "Points", None) or [])]
                for k in range(len(pts) - 1):
                    new_segments.append(_straight(pts[k], pts[k + 1]))
            elif parent.is_a("IfcTrimmedCurve"):
                basis = getattr(parent, "BasisCurve", None)
                if basis is not None and basis.is_a("IfcCircle"):
                    seg = _trimmed_circle_segment(parent, scale, angle_scale_deg)
                    if seg is not None:
                        new_segments.append(seg)
                elif basis is not None and basis.is_a("IfcLine"):
                    pts = _trimmed_line_points(parent)
                    if pts is not None:
                        new_segments.append(_straight(pts[0], pts[1]))
            if not same_sense:
                new_segments = [
                    Segment(
                        kind=s.kind, length_mm=s.length_mm,
                        start_mm=s.end_mm, end_mm=s.start_mm,
                        angle_deg=s.angle_deg, radius_mm=s.radius_mm,
                    )
                    for s in reversed(new_segments)
                ]
            segments.extend(new_segments)
        return segments

    if directrix.is_a("IfcPolyline"):
        pts = [_pt3(p.Coordinates) for p in (getattr(directrix, "Points", None) or [])]
        for k in range(len(pts) - 1):
            segments.append(_straight(pts[k], pts[k + 1]))
        return segments

    return []


def _trimmed_line_points(parent) -> Optional[tuple]:
    """Start/end points (model units) of an IfcTrimmedCurve over IfcLine."""
    basis = parent.BasisCurve
    pnt = _pt3(basis.Pnt.Coordinates)
    vec = basis.Dir
    direction = _pt3(vec.Orientation.DirectionRatios)
    magnitude = float(unwrap(getattr(vec, "Magnitude", 1.0)))

    def _resolve(trim_set):
        for val in trim_set or ():
            coords = getattr(val, "Coordinates", None)
            if coords is not None:
                return _pt3(coords)
        for val in trim_set or ():
            raw = unwrap(val)
            if isinstance(raw, (int, float)):
                return _add(pnt, _scale(_unit(direction), float(raw) * magnitude))
        return None

    p1 = _resolve(getattr(parent, "Trim1", None))
    p2 = _resolve(getattr(parent, "Trim2", None))
    if p1 is None or p2 is None:
        return None
    if not getattr(parent, "SenseAgreement", True):
        p1, p2 = p2, p1
    return (p1, p2)


def terminal_hook(bar, end: str = "end", unit_scale: Optional[float] = None) -> Optional[Hook]:
    """Terminal hook at the given end: (arc angle, straight tail beyond it).

    The tail is the sum of consecutive straight segments at the chosen end of
    the directrix; the hook bend is the arc immediately before them. Returns
    ``None`` when the end has no straight+arc pattern, or when the directrix
    is a CLOSED loop (start ~= end within 1 mm) — closed stirrups/ties have
    no free ends, so their corner bends are not terminal hooks.
    """
    segments = directrix_segments(bar, unit_scale=unit_scale)
    if not segments:
        return None
    if _dist(segments[0].start_mm, segments[-1].end_mm) <= 1.0:  # closed loop
        return None
    ordered = list(reversed(segments)) if end == "end" else list(segments)
    tail = 0.0
    i = 0
    while i < len(ordered) and ordered[i].kind == "straight":
        tail += ordered[i].length_mm
        i += 1
    if i == 0 or i >= len(ordered) or ordered[i].kind != "arc":
        return None
    arc = ordered[i]
    return Hook(angle_deg=arc.angle_deg, tail_mm=tail, radius_mm=arc.radius_mm, end=end)


# ---------------------------------------------------------------------------
# Attributes / properties
# ---------------------------------------------------------------------------

_NAME_DIA_RE = re.compile(r"(?:^|[^0-9])(\d{1,3})\s*(?:mm)\b", re.IGNORECASE)
_METRIC_CODE_RE = re.compile(r"(\d{1,3})\s*M\b")


def pset_names(element) -> list[str]:
    names = []
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pd = getattr(rel, "RelatingPropertyDefinition", None)
        if pd is not None and pd.is_a("IfcPropertySet"):
            names.append(str(getattr(pd, "Name", "") or ""))
    return names


def pset_value(element, pset_name: str, prop_name: str):
    """Value of one IfcPropertySingleValue in a named pset, or None."""
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pd = getattr(rel, "RelatingPropertyDefinition", None)
        if pd is None or not pd.is_a("IfcPropertySet"):
            continue
        if str(getattr(pd, "Name", "") or "") != pset_name:
            continue
        for prop in getattr(pd, "HasProperties", None) or []:
            if prop.is_a("IfcPropertySingleValue") and str(getattr(prop, "Name", "")) == prop_name:
                return unwrap(getattr(prop, "NominalValue", None))
    return None


def _pset_props(element) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pd = getattr(rel, "RelatingPropertyDefinition", None)
        if pd is None or not pd.is_a("IfcPropertySet"):
            continue
        for prop in getattr(pd, "HasProperties", None) or []:
            if prop.is_a("IfcPropertySingleValue") and getattr(prop, "Name", None):
                props[str(prop.Name)] = unwrap(getattr(prop, "NominalValue", None))
    return props


def _material_names(material) -> list[str]:
    """Flatten any IfcMaterialSelect variant to its material Name strings."""
    if material is None:
        return []
    if material.is_a("IfcMaterial"):
        return [str(getattr(material, "Name", "") or "")]
    if material.is_a("IfcMaterialList"):
        names: list[str] = []
        for m in getattr(material, "Materials", None) or []:
            names.extend(_material_names(m))
        return names
    if material.is_a("IfcMaterialLayerSetUsage"):
        return _material_names(getattr(material, "ForLayerSet", None))
    if material.is_a("IfcMaterialLayerSet"):
        names = []
        for layer in getattr(material, "MaterialLayers", None) or []:
            names.extend(_material_names(getattr(layer, "Material", None)))
        return names
    if material.is_a("IfcMaterialProfileSetUsage"):
        return _material_names(getattr(material, "ForProfileSet", None))
    if material.is_a("IfcMaterialProfileSet"):
        names = []
        for profile in getattr(material, "MaterialProfiles", None) or []:
            names.extend(_material_names(getattr(profile, "Material", None)))
        return names
    return []


def element_material_names(element) -> list[str]:
    """Material Name strings associated with the element (any Select variant)."""
    names: list[str] = []
    for rel in getattr(element, "HasAssociations", None) or []:
        if rel.is_a("IfcRelAssociatesMaterial"):
            names.extend(_material_names(getattr(rel, "RelatingMaterial", None)))
    return names


def profile_dims_mm(element, unit_scale: Optional[float] = None) -> Optional[tuple[float, float]]:
    """(XDim, YDim) in mm of the element's extruded rectangle profile, or None."""
    rep = getattr(element, "Representation", None)
    if not rep:
        return None
    scale = unit_scale if unit_scale is not None else unit_scale_mm(element.file)
    for shape_rep in getattr(rep, "Representations", None) or []:
        for item in getattr(shape_rep, "Items", None) or []:
            if not item.is_a("IfcExtrudedAreaSolid"):
                continue
            profile = getattr(item, "SweptArea", None)
            if profile is not None and profile.is_a("IfcRectangleProfileDef"):
                return (
                    float(unwrap(profile.XDim)) * scale,
                    float(unwrap(profile.YDim)) * scale,
                )
    return None


def bar_diameter_mm(bar, unit_scale: Optional[float] = None) -> Optional[float]:
    """Nominal bar diameter in mm.

    Resolution order: ``NominalDiameter`` attribute (scaled by the model
    length unit), then diameter-bearing pset keys (``Reference``,
    ``BarReference``, ``Bar Size``), then a ``<NN>mm`` / ``<NN>M`` regex over
    ``Name``/``ObjectType``.
    """
    scale = unit_scale if unit_scale is not None else unit_scale_mm(bar.file)

    nominal = getattr(bar, "NominalDiameter", None)
    if nominal is not None:
        try:
            dia = float(unwrap(nominal)) * scale
            if 1.0 <= dia <= 200.0:
                return dia
        except (TypeError, ValueError):
            pass

    props = _pset_props(bar)
    for key in ("Reference", "BarReference", "Bar Size"):
        val = props.get(key)
        if val is None:
            continue
        try:
            f = float(val)
            if 3.0 <= f <= 80.0:
                return f
        except (TypeError, ValueError):
            pass
        m = _NAME_DIA_RE.search(str(val)) or _METRIC_CODE_RE.search(str(val))
        if m:
            f = float(m.group(1))
            if 3.0 <= f <= 80.0:
                return f

    for attr in ("Name", "ObjectType"):
        raw = getattr(bar, attr, None)
        if not raw:
            continue
        m = _NAME_DIA_RE.search(str(raw)) or _METRIC_CODE_RE.search(str(raw))
        if m:
            f = float(m.group(1))
            if 3.0 <= f <= 80.0:
                return f
    return None


# ---------------------------------------------------------------------------
# Spatial structure / placement
# ---------------------------------------------------------------------------

def element_storey_name(element) -> Optional[str]:
    for rel in getattr(element, "ContainedInStructure", None) or []:
        structure = getattr(rel, "RelatingStructure", None)
        if structure is not None and structure.is_a("IfcBuildingStorey"):
            return str(getattr(structure, "Name", None) or getattr(structure, "LongName", "") or "")
    return None


def find_storey(model, name: str):
    """Find an IfcBuildingStorey by exact then case-insensitive-substring name."""
    storeys = model.by_type("IfcBuildingStorey")
    for s in storeys:
        if (getattr(s, "Name", None) or "") == name:
            return s
    lowered = name.lower()
    for s in storeys:
        if lowered in str(getattr(s, "Name", "") or "").lower():
            return s
    return None


def element_by_guid(model, guid: str):
    try:
        return model.by_guid(guid)
    except Exception:
        return None


def placement_matrix(element):
    """4x4 world placement matrix (numpy) of the element, or None."""
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    import ifcopenshell.util.placement
    return ifcopenshell.util.placement.get_local_placement(placement)


def placement_origin_mm(element) -> Optional[tuple[float, float, float]]:
    m = placement_matrix(element)
    if m is None:
        return None
    scale = unit_scale_mm(element.file)
    return (float(m[0][3]) * scale, float(m[1][3]) * scale, float(m[2][3]) * scale)


def _transform(matrix, p):
    x, y, z = p
    return (
        float(matrix[0][0]) * x + float(matrix[0][1]) * y + float(matrix[0][2]) * z + float(matrix[0][3]),
        float(matrix[1][0]) * x + float(matrix[1][1]) * y + float(matrix[1][2]) * z + float(matrix[1][3]),
        float(matrix[2][0]) * x + float(matrix[2][1]) * y + float(matrix[2][2]) * z + float(matrix[2][3]),
    )


def _directrix_points_local(bar) -> list[tuple[float, float, float]]:
    """Directrix polygon points in local model units (arcs contribute endpoints)."""
    pts: list[tuple[float, float, float]] = []
    scale = unit_scale_mm(bar.file)
    for seg in directrix_segments(bar):
        for p in (seg.start_mm, seg.end_mm):
            local = _scale(p, 1.0 / scale)
            if not pts or _dist(pts[-1], local) > 1e-9:
                pts.append(local)
    return pts


def directrix_points_world_mm(bar) -> list[tuple[float, float, float]]:
    """Directrix segment endpoints of the bar in world millimetres."""
    matrix = placement_matrix(bar)
    if matrix is None:
        return []
    scale = unit_scale_mm(bar.file)
    return [_scale(_transform(matrix, p), scale) for p in _directrix_points_local(bar)]


def element_bbox_mm(element):
    """Best-effort world axis-aligned bbox in mm from placement + profile.

    Supports ``IfcExtrudedAreaSolid`` over ``IfcRectangleProfileDef`` (and
    polyline-bounded ``IfcArbitraryClosedProfileDef``) and swept-disk bars
    (directrix points padded by the disk radius). Returns ``None`` for
    anything else — this is a fixture oracle, not a geometry engine.
    """
    matrix = placement_matrix(element)
    if matrix is None:
        return None
    scale = unit_scale_mm(element.file)
    corners: list[tuple[float, float, float]] = []
    pad = 0.0

    solid = find_swept_disk(element)
    if solid is not None:
        pts = _directrix_points_local(element)
        if not pts:
            return None
        pad = float(unwrap(getattr(solid, "Radius", 0.0))) or 0.0
        corners = pts
    else:
        rep = getattr(element, "Representation", None)
        if not rep:
            return None
        extrusion = None
        for shape_rep in getattr(rep, "Representations", None) or []:
            for item in getattr(shape_rep, "Items", None) or []:
                if item.is_a("IfcExtrudedAreaSolid"):
                    extrusion = item
                    break
            if extrusion is not None:
                break
        if extrusion is None:
            return None
        profile = extrusion.SweptArea
        pts2d: list[tuple[float, float]] = []
        if profile.is_a("IfcRectangleProfileDef"):
            hx = float(unwrap(profile.XDim)) / 2.0
            hy = float(unwrap(profile.YDim)) / 2.0
            pts2d = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        elif profile.is_a("IfcArbitraryClosedProfileDef"):
            outer = getattr(profile, "OuterCurve", None)
            if outer is not None and outer.is_a("IfcPolyline"):
                pts2d = [(float(p.Coordinates[0]), float(p.Coordinates[1])) for p in outer.Points]
        if not pts2d:
            return None
        depth = float(unwrap(extrusion.Depth))
        dir_local = _unit(_pt3(extrusion.ExtrudedDirection.DirectionRatios))
        origin, x_hat, y_hat, z_hat = _axis2_frame(extrusion.Position)
        for (px, py) in pts2d:
            base = _add(origin, _add(_scale(x_hat, px), _scale(y_hat, py)))
            top_offset = _scale(
                _add(_scale(x_hat, dir_local[0] * depth),
                     _add(_scale(y_hat, dir_local[1] * depth), _scale(z_hat, dir_local[2] * depth))),
                1.0,
            )
            corners.append(base)
            corners.append(_add(base, top_offset))

    world = [_transform(matrix, p) for p in corners]
    xs = [p[0] for p in world]
    ys = [p[1] for p in world]
    zs = [p[2] for p in world]
    return (
        ((min(xs) - pad) * scale, (min(ys) - pad) * scale, (min(zs) - pad) * scale),
        ((max(xs) + pad) * scale, (max(ys) + pad) * scale, (max(zs) + pad) * scale),
    )
