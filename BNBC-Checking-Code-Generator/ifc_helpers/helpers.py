"""Shared helper library for BNBC IFC rule checkers.

This module eliminates code duplication across checker scripts by
providing robust, schema-tolerant utilities for:
- IFC value unwrapping and unit conversion
- Element identification and storey resolution
- Bounding-box extraction from geometry
- Reinforcement bar classification and diameter parsing
- Concrete / steel material strength extraction
- Property-set traversal
- Geometry-settings factory

All functions are safe for both IFC2X3 and IFC4 models.  No NumPy
dependency — only the Python standard library and *ifcopenshell*.
"""

from __future__ import annotations

import logging
import math
import re
import sys
from typing import Any, Dict, Optional, Tuple

import ifcopenshell
import ifcopenshell.geom

_LOGGER = logging.getLogger(__name__)


# ── SI prefix multipliers (to base unit, i.e. metres) ────────────────
_SI_PREFIX_FACTOR: Dict[str, float] = {
    "EXA":   1e18,
    "PETA":  1e15,
    "TERA":  1e12,
    "GIGA":  1e9,
    "MEGA":  1e6,
    "KILO":  1e3,
    "HECTO": 1e2,
    "DECA":  1e1,
    "DECI":  1e-1,
    "CENTI": 1e-2,
    "MILLI": 1e-3,
    "MICRO": 1e-6,
    "NANO":  1e-9,
}


# ─────────────────────────────────────────────────────────────────────
# 1. Value unwrapping
# ─────────────────────────────────────────────────────────────────────
def unwrap(value: Any) -> Any:
    """Unwrap an IFC wrapped value (e.g. ``IfcReal``, ``IfcInteger``).

    Parameters
    ----------
    value:
        Any value that may or may not have a ``wrappedValue`` attribute.

    Returns
    -------
    Any
        The underlying Python primitive, or *value* unchanged.
    """
    return getattr(value, "wrappedValue", value)


# ─────────────────────────────────────────────────────────────────────
# 2. Length-unit scale factor  →  millimetres
# ─────────────────────────────────────────────────────────────────────
def length_unit_to_mm(model: ifcopenshell.file) -> float:
    """Return the scale factor that converts model length units to **mm**.

    The function inspects ``IfcProject → UnitsInContext → Units`` for
    the ``LENGTHUNIT`` assignment.  It handles:

    * ``IfcSIUnit`` with ``METRE`` / ``METER`` name and any SI prefix
      from ``EXA`` down to ``NANO``.
    * ``IfcConversionBasedUnit`` (e.g. inches, feet) by reading the
      ``ConversionFactor`` value.
    * **Falls back to 1.0 (i.e. assumes millimetres)** when no usable
      length-unit assignment is found, and logs a warning.  Millimetres
      are the dominant authoring convention for the structural/rebar
      models this library targets; silently assuming metres (the old
      behaviour, factor 1000.0) produced catastrophic 1000x errors on
      unit-less mm models.  If your unit-less model is actually in
      metres, pass an explicit scale to downstream helpers instead of
      relying on this default.

    Parameters
    ----------
    model:
        An open ``ifcopenshell.file`` handle.

    Returns
    -------
    float
        Multiply any raw model coordinate/dimension by this factor to
        obtain the value in millimetres.
    """
    projects = model.by_type("IfcProject")
    if not projects:
        _LOGGER.warning(
            "length_unit_to_mm: model has no IfcProject; assuming "
            "millimetres (scale 1.0)."
        )
        return 1.0

    project = projects[0]
    units_in_context = getattr(project, "UnitsInContext", None)
    if units_in_context is None:
        _LOGGER.warning(
            "length_unit_to_mm: IfcProject has no UnitsInContext; "
            "assuming millimetres (scale 1.0)."
        )
        return 1.0

    units = getattr(units_in_context, "Units", None) or []
    for unit in units:
        unit_type = getattr(unit, "UnitType", None)
        if unit_type != "LENGTHUNIT":
            continue

        # ── IfcSIUnit ────────────────────────────────────────────
        if unit.is_a("IfcSIUnit"):
            name = getattr(unit, "Name", "") or ""
            if name.upper() not in ("METRE", "METER"):
                continue
            prefix = getattr(unit, "Prefix", None)
            if prefix is None:
                # Base unit is metre → 1 m = 1000 mm
                return 1000.0
            factor = _SI_PREFIX_FACTOR.get(prefix.upper(), 1.0)
            # factor converts prefixed unit → metres; then × 1000 → mm
            return factor * 1000.0

        # ── IfcConversionBasedUnit ───────────────────────────────
        if unit.is_a("IfcConversionBasedUnit"):
            conversion_factor = getattr(unit, "ConversionFactor", None)
            if conversion_factor is not None:
                value_component = getattr(
                    conversion_factor, "ValueComponent", None
                )
                if value_component is not None:
                    # ValueComponent gives metres-per-unit
                    metres_per_unit = float(unwrap(value_component))
                    return metres_per_unit * 1000.0
            # Fallback: try recognising well-known names
            unit_name = (getattr(unit, "Name", "") or "").upper()
            if "INCH" in unit_name:
                return 25.4
            if "FOOT" in unit_name or "FEET" in unit_name:
                return 304.8

    _LOGGER.warning(
        "length_unit_to_mm: no usable LENGTHUNIT assignment found; "
        "assuming millimetres (scale 1.0)."
    )
    return 1.0  # default on missing units: millimetres


# ─────────────────────────────────────────────────────────────────────
# 3. Element label
# ─────────────────────────────────────────────────────────────────────
def element_label(element: ifcopenshell.entity_instance) -> str:
    """Return a human-readable label in ``"Name (GlobalId)"`` format.

    Falls back to ``ObjectType`` when ``Name`` is empty, and to
    ``"Unnamed"`` when both are absent.  Uses ``GlobalId`` when
    available, otherwise ``#<entity-id>``.

    Parameters
    ----------
    element:
        Any IFC entity instance.

    Returns
    -------
    str
        Formatted element label.
    """
    name = getattr(element, "Name", None) or getattr(
        element, "ObjectType", None
    )
    if not name:
        name = "Unnamed"

    gid = getattr(element, "GlobalId", None)
    if not gid:
        gid = f"#{element.id()}"

    return f"{name} ({gid})"


# ─────────────────────────────────────────────────────────────────────
# 4. Building storey of an element
# ─────────────────────────────────────────────────────────────────────
def element_storey(element: ifcopenshell.entity_instance) -> str:
    """Return the building-storey name that spatially contains *element*.

    Resolution strategy:

    1. Traverse ``ContainedInStructure`` inverse relationships and
       return the ``Name`` (or ``LongName``) of the relating spatial
       structure (typically an ``IfcBuildingStorey``).
    2. If the element is not directly contained (common for rebar that
       is *aggregated* into a host element rather than placed in a
       storey), walk the ``Decomposes`` (``IfcRelAggregates`` /
       ``IfcRelNests``) chain upward: if an ancestor is an
       ``IfcBuildingStorey`` its name is returned, otherwise each
       ancestor's own ``ContainedInStructure`` is checked.  The walk is
       capped at 10 levels to guard against cyclic data.

    Parameters
    ----------
    element:
        Any IFC product entity.

    Returns
    -------
    str
        Storey name, or ``"Unknown level"`` when none is found.
    """

    def _structure_name(structure: Any) -> Optional[str]:
        name = getattr(structure, "Name", None) or getattr(
            structure, "LongName", None
        )
        return str(name) if name else None

    def _containment_name(obj: Any) -> Optional[str]:
        contained = getattr(obj, "ContainedInStructure", None) or []
        for rel in contained:
            structure = getattr(rel, "RelatingStructure", None)
            if structure is None:
                continue
            name = _structure_name(structure)
            if name:
                return name
        return None

    # ── 1. Direct spatial containment ────────────────────────────
    name = _containment_name(element)
    if name:
        return name

    # ── 2. Walk decomposition (IfcRelAggregates / IfcRelNests) up ─
    current = element
    visited = set()
    for _ in range(10):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))

        parent = None
        for rel_attr in ("Decomposes", "Nests"):
            rels = getattr(current, rel_attr, None) or []
            for rel in rels:
                candidate = getattr(rel, "RelatingObject", None)
                if candidate is not None:
                    parent = candidate
                    break
            if parent is not None:
                break

        if parent is None:
            break

        if parent.is_a("IfcBuildingStorey"):
            name = _structure_name(parent)
            if name:
                return name

        name = _containment_name(parent)
        if name:
            return name

        current = parent

    return "Unknown level"


# ─────────────────────────────────────────────────────────────────────
# 5. Bounding-box extraction (mm)
# ─────────────────────────────────────────────────────────────────────
def get_bbox_mm(
    shape: Any,
) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
    """Extract the axis-aligned bounding box from an *ifcopenshell.geom* shape.

    The geometry engine returns vertex coordinates in **metres**.  This
    function multiplies by 1 000 to return values in **mm**.

    Parameters
    ----------
    shape:
        The shape object returned by
        ``ifcopenshell.geom.create_shape(settings, element)``.

    Returns
    -------
    tuple or None
        ``((x_min, y_min, z_min), (x_max, y_max, z_max))`` in mm, or
        ``None`` if the shape has no vertices.
    """
    try:
        geometry = shape.geometry
        verts = geometry.verts
    except AttributeError:
        return None

    if not verts:
        return None

    # verts is a flat list: [x0, y0, z0, x1, y1, z1, ...]
    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]

    # Geometry engine outputs metres → convert to mm
    return (
        (min(xs) * 1000.0, min(ys) * 1000.0, min(zs) * 1000.0),
        (max(xs) * 1000.0, max(ys) * 1000.0, max(zs) * 1000.0),
    )


# ─────────────────────────────────────────────────────────────────────
# 6. Stirrup / tie detection
# ─────────────────────────────────────────────────────────────────────
_STIRRUP_SUBSTRING_KEYWORDS = ("stirrup", "tie", "link", "hoop")
# Word-boundary patterns for the short designations.  An underscore or
# any other non-alphanumeric character counts as a boundary (so
# "M_T1" matches) but embedding inside a longer alphanumeric token does
# not (so "t1" inside "t100" or "start"-style words never matches).
_STIRRUP_T_PATTERN = re.compile(r"(?<![a-z0-9])t[1-9](?![a-z0-9])")
_STIRRUP_MT_PATTERN = re.compile(r"(?<![a-z0-9])m_t(?![a-z0-9])")


def is_stirrup_or_tie(bar: ifcopenshell.entity_instance) -> bool:
    """Heuristically decide whether *bar* is a stirrup / tie / link.

    Checks both ``Name`` and ``ObjectType`` (case-insensitive):

    * Plain substring match for the full words ``stirrup``, ``tie``,
      ``link``, ``hoop``.
    * **Word-boundary** match for the short designations ``t1``–``t9``
      and ``m_t`` (underscores count as boundaries, so ``"Shape M_T1"``
      matches while ``"t1"`` buried inside a longer token such as
      ``"t100"`` does not).

    .. note::
       The ``t1``–``t9`` / ``m_t`` keywords are **dataset-specific
       Revit shape-naming heuristics** (e.g. family types named
       ``"M_T1"``, ``"T3"``) — they are not a general IFC convention
       and may need adjustment for other authoring tools.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity (``IfcReinforcingBar`` or similar).

    Returns
    -------
    bool
        ``True`` when the bar appears to be transverse reinforcement.
    """
    for attr_name in ("Name", "ObjectType"):
        raw = getattr(bar, attr_name, None)
        if not raw:
            continue
        text = str(raw).lower()
        if any(kw in text for kw in _STIRRUP_SUBSTRING_KEYWORDS):
            return True
        if _STIRRUP_T_PATTERN.search(text):
            return True
        if _STIRRUP_MT_PATTERN.search(text):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 7. Bar diameter (mm)
# ─────────────────────────────────────────────────────────────────────
# "16mm" / "12 mm" — an explicit millimetre suffix is required; a bare
# "m" is NOT accepted because it false-matches length tokens such as
# "2 m" (metres).
_DIAMETER_NAME_RE = re.compile(
    r"(?:^|[^0-9])(\d{1,3})\s*mm(?![a-zA-Z])", re.IGNORECASE
)
# "16M" / "25M" metric bar-designation codes: digits immediately
# followed by an UPPERCASE "M" (no whitespace, case-sensitive), so that
# "2 m" / "20 m" length tokens never match.
_DIAMETER_METRIC_CODE_RE = re.compile(
    r"(?:^|[^0-9A-Za-z])(\d{1,3})M(?![a-zA-Z0-9])"
)


def get_bar_diameter_mm(
    bar: ifcopenshell.entity_instance,
    unit_scale: Optional[float] = None,
) -> Optional[float]:
    """Return the nominal diameter of a reinforcing bar in **mm**.

    Resolution order:

    1. Property sets — look for ``Reference``, ``BarReference``, or
       ``Bar Size`` properties that embed a numeric diameter.
    2. ``NominalDiameter`` attribute multiplied by *unit_scale*.
    3. Name / ObjectType regex for patterns like ``"25M"`` or
       ``"16mm"``.

    String parsing accepts only an explicit ``mm`` suffix or a metric
    bar-designation code (digits immediately followed by uppercase
    ``M``, e.g. ``"20M"``).  A bare ``m`` is deliberately rejected so
    that length tokens such as ``"2 m"`` are never mistaken for a
    diameter.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity.
    unit_scale:
        The scale factor from :func:`length_unit_to_mm`.  When omitted
        it is auto-detected from the bar's parent model.

    Returns
    -------
    float or None
        Bar diameter in millimetres, or ``None`` when undetermined.
    """
    if unit_scale is None:
        try:
            unit_scale = length_unit_to_mm(bar.file)
        except Exception:
            unit_scale = 1.0

    def _diameter_from_text(text: str) -> Optional[float]:
        m = _DIAMETER_NAME_RE.search(text)
        if m:
            fval = float(m.group(1))
            if 3.0 <= fval <= 80.0:
                return fval
        m = _DIAMETER_METRIC_CODE_RE.search(text)
        if m:
            fval = float(m.group(1))
            if 3.0 <= fval <= 80.0:
                return fval
        return None

    # ── (a) Property-set scan ────────────────────────────────────
    psets = property_sets(bar)
    _PSET_KEYS = ("Reference", "BarReference", "Bar Size")
    for _pset_name, props in psets.items():
        for key in _PSET_KEYS:
            val = props.get(key)
            if val is None:
                continue
            val = unwrap(val)
            # Might be numeric already
            try:
                fval = float(val)
                if 3.0 <= fval <= 80.0:
                    return fval
            except (TypeError, ValueError):
                pass
            # Try regex on string representation
            fval = _diameter_from_text(str(val))
            if fval is not None:
                return fval

    # ── (b) NominalDiameter attribute ────────────────────────────
    nominal = getattr(bar, "NominalDiameter", None)
    if nominal is not None:
        nominal = unwrap(nominal)
        try:
            dia_mm = float(nominal) * unit_scale
            if 1.0 <= dia_mm <= 200.0:
                return dia_mm
        except (TypeError, ValueError):
            pass

    # ── (c) Name / ObjectType regex ──────────────────────────────
    for attr_name in ("Name", "ObjectType"):
        raw = getattr(bar, attr_name, None)
        if not raw:
            continue
        fval = _diameter_from_text(str(raw))
        if fval is not None:
            return fval

    return None


# ─────────────────────────────────────────────────────────────────────
# 8. Concrete fc  &  Steel fy  (MPa)
# ─────────────────────────────────────────────────────────────────────
_CONCRETE_GRADE_RE = re.compile(
    r"(?:C|M)\s*(\d{2,3})", re.IGNORECASE
)
_CONCRETE_MPA_RE = re.compile(
    r"(\d{2,3})\s*MPa", re.IGNORECASE
)
_STEEL_GRADE_RE = re.compile(
    r"[Gg]rade\s*(\d{3,4})"
)
_STEEL_FY_RE = re.compile(
    r"(\d{3,4})\s*MPa", re.IGNORECASE
)


def get_material_fc_fy(
    model: ifcopenshell.file,
) -> Tuple[float, float]:
    """Extract concrete compressive strength *f'c* and steel yield
    strength *fy* from the model's ``IfcMaterial`` entities.

    Heuristics applied:

    * **Concrete**: match ``C25``, ``M25``, or ``<digits> MPa`` in the
      material name.  The first match is used.
    * **Steel**: match ``Grade 420`` or ``<digits> MPa`` patterns.

    Parameters
    ----------
    model:
        An open ``ifcopenshell.file`` handle.

    Returns
    -------
    tuple[float, float]
        ``(fc_mpa, fy_mpa)``.  Defaults to ``(28.0, 420.0)`` for any
        value that cannot be determined.
    """
    fc: Optional[float] = None
    fy: Optional[float] = None

    try:
        materials = model.by_type("IfcMaterial")
    except Exception:
        return (28.0, 420.0)

    for mat in materials:
        name = getattr(mat, "Name", None)
        if not name:
            continue
        name_str = str(name)

        # ── Concrete ────────────────────────────────────────────
        if fc is None:
            m = _CONCRETE_GRADE_RE.search(name_str)
            if m:
                fc = float(m.group(1))
            else:
                m = _CONCRETE_MPA_RE.search(name_str)
                if m:
                    fc = float(m.group(1))

        # ── Steel ────────────────────────────────────────────────
        if fy is None:
            m = _STEEL_GRADE_RE.search(name_str)
            if m:
                fy = float(m.group(1))
            else:
                m = _STEEL_FY_RE.search(name_str)
                if m:
                    candidate = float(m.group(1))
                    # Distinguish from concrete MPa values
                    if candidate >= 200.0:
                        fy = candidate

    return (fc if fc is not None else 28.0, fy if fy is not None else 420.0)


# ─────────────────────────────────────────────────────────────────────
# 9. Slab / area reinforcement detection
# ─────────────────────────────────────────────────────────────────────
_AREA_KEYWORDS = {"area", "path", "fabric", "mesh", "slab", "wall"}


def is_slab_area_reinforcement(
    bar: ifcopenshell.entity_instance,
    model: Optional[ifcopenshell.file] = None,
) -> bool:
    """Determine whether *bar* belongs to slab / wall area reinforcement.

    Inspects inverse relationships:

    * ``IfcRelAssignsToGroup`` — check group name / description for
      area-related keywords (area, path, fabric, mesh, slab, wall).
    * ``IfcRelContainedInSpatialStructure`` — check whether the
      relating structure is an ``IfcSlab`` or ``IfcWall``.
    * ``IfcRelAggregates`` / ``IfcRelNests`` — walk up the
      decomposition tree looking for slab or wall parents.

    Parameters
    ----------
    model:
        An open ``ifcopenshell.file`` handle (needed for inverse
        look-ups in some schema versions).
    bar:
        A reinforcing-bar entity.

    Returns
    -------
    bool
        ``True`` when the bar appears to be slab/wall area
        reinforcement.
    """
    # ── IfcRelAssignsToGroup ─────────────────────────────────────
    assignments = getattr(bar, "HasAssignments", None) or []
    for rel in assignments:
        if not rel.is_a("IfcRelAssignsToGroup"):
            continue
        group = getattr(rel, "RelatingGroup", None)
        if group is None:
            continue
        for attr in ("Name", "Description", "ObjectType"):
            text = getattr(group, attr, None)
            if not text:
                continue
            lower = str(text).lower()
            if any(kw in lower for kw in _AREA_KEYWORDS):
                return True

    # ── IfcRelContainedInSpatialStructure ────────────────────────
    contained = getattr(bar, "ContainedInStructure", None) or []
    for rel in contained:
        structure = getattr(rel, "RelatingStructure", None)
        if structure is not None:
            if structure.is_a("IfcSlab") or structure.is_a("IfcWall"):
                return True

    # ── IfcRelAggregates / IfcRelNests — walk upward ─────────────
    for rel_attr in ("Decomposes", "Nests"):
        decompositions = getattr(bar, rel_attr, None) or []
        for rel in decompositions:
            parent = getattr(rel, "RelatingObject", None)
            if parent is None:
                continue
            if parent.is_a("IfcSlab") or parent.is_a("IfcWall"):
                return True
            # One more level up (e.g. rebar → rebar group → slab)
            for inner_attr in ("Decomposes", "Nests"):
                inner_rels = getattr(parent, inner_attr, None) or []
                for inner_rel in inner_rels:
                    grandparent = getattr(
                        inner_rel, "RelatingObject", None
                    )
                    if grandparent is None:
                        continue
                    if grandparent.is_a("IfcSlab") or grandparent.is_a(
                        "IfcWall"
                    ):
                        return True

    return False


# ─────────────────────────────────────────────────────────────────────
# 10. Property-set extraction
# ─────────────────────────────────────────────────────────────────────
def property_sets(
    element: ifcopenshell.entity_instance,
) -> Dict[str, Dict[str, Any]]:
    """Return all ``IfcPropertySet`` data attached to *element*.

    Iterates ``IsDefinedBy`` → ``IfcRelDefinesByProperties`` →
    ``IfcPropertySet`` → ``HasProperties`` →
    ``IfcPropertySingleValue``.

    Parameters
    ----------
    element:
        Any IFC product entity.

    Returns
    -------
    dict[str, dict[str, Any]]
        Nested mapping ``{pset_name: {prop_name: prop_value}}``.
        Values are unwrapped via :func:`unwrap`.
    """
    result: Dict[str, Dict[str, Any]] = {}

    is_defined_by = getattr(element, "IsDefinedBy", None) or []
    for rel in is_defined_by:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pset = getattr(rel, "RelatingPropertyDefinition", None)
        if pset is None or not pset.is_a("IfcPropertySet"):
            continue
        pset_name = getattr(pset, "Name", None) or "Unnamed"
        props: Dict[str, Any] = {}
        has_properties = getattr(pset, "HasProperties", None) or []
        for prop in has_properties:
            if not prop.is_a("IfcPropertySingleValue"):
                continue
            prop_name = getattr(prop, "Name", None)
            if prop_name is None:
                continue
            nominal = getattr(prop, "NominalValue", None)
            props[str(prop_name)] = unwrap(nominal)
        result[str(pset_name)] = props

    return result


# ─────────────────────────────────────────────────────────────────────
# 11. Geometry settings factory
# ─────────────────────────────────────────────────────────────────────
def geom_settings(use_world_coords: bool = True) -> Any:
    """Create an ``ifcopenshell.geom.settings`` object.

    Parameters
    ----------
    use_world_coords:
        When ``True`` (default), shapes are returned in absolute world
        coordinates rather than element-local placement.

    Returns
    -------
    ifcopenshell.geom.settings
        Ready-to-use settings instance.
    """
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, use_world_coords)
    return settings


# ─────────────────────────────────────────────────────────────────────
# 12. High-Performance Rebar Geometry parsing (avoiding create_shape)
# ─────────────────────────────────────────────────────────────────────
def get_bar_placement_fast(bar: ifcopenshell.entity_instance) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
    """Extract local placement origin and direction vector in native project units.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity.

    Returns
    -------
    tuple or None
        ((x_origin, y_origin, z_origin), (x_dir, y_dir, z_dir)) or None.
    """
    local_pts = get_bar_directrix_points(bar)
    placement = getattr(bar, "ObjectPlacement", None)
    
    # Try fast path using directrix
    if local_pts and placement:
        import ifcopenshell.util.placement
        import numpy as np
        try:
            matrix = ifcopenshell.util.placement.get_local_placement(placement)
            # Calculate first point in world coordinates
            pt_3d = list(local_pts[0])
            while len(pt_3d) < 3:
                pt_3d.append(0.0)
            v1 = np.array([pt_3d[0], pt_3d[1], pt_3d[2], 1.0])
            origin = (matrix @ v1)[:3].tolist()

            # Calculate direction vector
            if len(local_pts) >= 2:
                pt_last = list(local_pts[-1])
                while len(pt_last) < 3:
                    pt_last.append(0.0)
                v2 = np.array([pt_last[0], pt_last[1], pt_last[2], 1.0])
                end_pt = (matrix @ v2)[:3]
                direction = (end_pt - np.array(origin))
                norm = np.linalg.norm(direction)
                if norm > 1e-6:
                    direction = (direction / norm).tolist()
                else:
                    direction = [0.0, 0.0, 1.0]
            else:
                direction = matrix[:3, 2].tolist()

            return (tuple(origin), tuple(direction))
        except Exception:
            pass

    # Fallback path using create_shape
    import ifcopenshell.geom
    try:
        unit_scale = length_unit_to_mm(bar.file)
        # scale factor from meters (create_shape unit) to native project unit:
        # if native is mm (unit_scale=1.0), we multiply by 1000.0
        # if native is meters (unit_scale=1000.0), we multiply by 1.0
        to_native = 1000.0 / unit_scale
        
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        shape = ifcopenshell.geom.create_shape(settings, bar)
        verts = shape.geometry.verts
        pts = [verts[i : i + 3] for i in range(0, len(verts), 3)]
        if pts:
            xs = [p[0] * to_native for p in pts]
            ys = [p[1] * to_native for p in pts]
            zs = [p[2] * to_native for p in pts]
            min_pt = (min(xs), min(ys), min(zs))
            max_pt = (max(xs), max(ys), max(zs))
            
            origin = (
                (min_pt[0] + max_pt[0]) / 2.0,
                (min_pt[1] + max_pt[1]) / 2.0,
                (min_pt[2] + max_pt[2]) / 2.0
            )
            
            # Estimate direction vector from bbox sizes
            dx = max_pt[0] - min_pt[0]
            dy = max_pt[1] - min_pt[1]
            dz = max_pt[2] - min_pt[2]
            
            dims = [dx, dy, dz]
            max_dim = max(dims)
            direction = [0.0, 0.0, 0.0]
            if max_dim > 1e-6:
                idx = dims.index(max_dim)
                direction[idx] = 1.0
            else:
                direction = [0.0, 0.0, 1.0]
                
            return (origin, tuple(direction))
    except Exception:
        pass
        
    return None


def _plane_angle_unit_is_degrees(model) -> bool:
    """Return True if the model's plane angle unit is degrees, False if radians."""
    projects = getattr(model, "by_type", lambda t: [])("IfcProject")
    if not projects:
        return True
    project = projects[0]
    units_in_context = getattr(project, "UnitsInContext", None)
    if units_in_context is None:
        return True
    units = getattr(units_in_context, "Units", None) or []
    for unit in units:
        unit_type = getattr(unit, "UnitType", None)
        if unit_type != "PLANEANGLEUNIT":
            continue
        if unit.is_a("IfcSIUnit"):
            name = getattr(unit, "Name", "") or ""
            if name.upper() == "RADIAN":
                return False
        elif unit.is_a("IfcConversionBasedUnit"):
            unit_name = (getattr(unit, "Name", "") or "").upper()
            if "DEGREE" in unit_name or "DEG" in unit_name:
                return True
            conversion_factor = getattr(unit, "ConversionFactor", None)
            if conversion_factor is not None:
                value_component = getattr(conversion_factor, "ValueComponent", None)
                if value_component is not None:
                    rad_val = float(unwrap(value_component))
                    if abs(rad_val - 0.017453292519943295) < 1e-4:
                        return True
    return True


def _discretize_trimmed_circle(trimmed_curve, num_points=8):
    """Discretize an IfcTrimmedCurve with IfcCircle basis into arc points.

    Parameters
    ----------
    trimmed_curve:
        An IfcTrimmedCurve whose BasisCurve is an IfcCircle.
    num_points:
        Number of sample points along the arc (including endpoints).

    Returns
    -------
    list of tuple
        List of (x, y, z) tuples along the arc.
    """
    basis = getattr(trimmed_curve, "BasisCurve", None)
    if basis is None:
        return []

    radius = float(unwrap(getattr(basis, "Radius", 0.0)))
    if radius <= 0.0:
        return []

    position = getattr(basis, "Position", None)
    if position is None:
        return []

    # ── Extract centre point ──────────────────────────────────────
    loc = getattr(position, "Location", None)
    if loc is None:
        return []
    loc_coords = list(getattr(loc, "Coordinates", (0.0, 0.0, 0.0)))
    while len(loc_coords) < 3:
        loc_coords.append(0.0)
    cx, cy, cz = loc_coords

    # ── Determine if 2D or 3D placement ───────────────────────────
    is_3d = position.is_a("IfcAxis2Placement3D")

    # Local X-axis (RefDirection)
    ref_dir = getattr(position, "RefDirection", None)
    if ref_dir is not None:
        rd = list(getattr(ref_dir, "DirectionRatios", (1.0, 0.0, 0.0)))
        while len(rd) < 3:
            rd.append(0.0)
    else:
        rd = [1.0, 0.0, 0.0]

    # Local Z-axis (Axis) — only for 3D placements
    if is_3d:
        axis_attr = getattr(position, "Axis", None)
        if axis_attr is not None:
            az = list(getattr(axis_attr, "DirectionRatios", (0.0, 0.0, 1.0)))
            while len(az) < 3:
                az.append(0.0)
        else:
            az = [0.0, 0.0, 1.0]
    else:
        az = [0.0, 0.0, 1.0]

    # Normalise local Z
    az_len = math.sqrt(az[0] ** 2 + az[1] ** 2 + az[2] ** 2)
    if az_len < 1e-12:
        az = [0.0, 0.0, 1.0]
        az_len = 1.0
    az = [a / az_len for a in az]

    # Gram–Schmidt: local_x = rd - (rd·az)*az, then normalise
    dot_rd_az = rd[0] * az[0] + rd[1] * az[1] + rd[2] * az[2]
    lx = [rd[i] - dot_rd_az * az[i] for i in range(3)]
    lx_len = math.sqrt(lx[0] ** 2 + lx[1] ** 2 + lx[2] ** 2)
    if lx_len < 1e-12:
        lx = [1.0, 0.0, 0.0]
        lx_len = 1.0
    lx = [v / lx_len for v in lx]

    # Local Y = Z × X
    ly = [
        az[1] * lx[2] - az[2] * lx[1],
        az[2] * lx[0] - az[0] * lx[2],
        az[0] * lx[1] - az[1] * lx[0],
    ]

    # ── Read trim parameters ──────────────────────────────────────
    trim1 = getattr(trimmed_curve, "Trim1", None) or ()
    trim2 = getattr(trimmed_curve, "Trim2", None) or ()
    sense = getattr(trimmed_curve, "SenseAgreement", True)

    model = getattr(trimmed_curve, "file", None)
    is_degrees = _plane_angle_unit_is_degrees(model) if model else True

    def _extract_param(trim_set):
        """Extract a parameter angle (radians) from a trim specification."""
        for val in trim_set:
            raw = unwrap(val)
            if isinstance(raw, (int, float)):
                val_float = float(raw)
                # Convert IfcParameterValue in degrees to radians
                if is_degrees:
                    return math.radians(val_float)
                return val_float
        for val in trim_set:
            coords = getattr(val, "Coordinates", None)
            if coords is not None:
                c = list(coords)
                dx = float(c[0]) - cx
                dy = float(c[1]) - cy if len(c) > 1 else 0.0
                return math.atan2(dy, dx)
        return None

    t1 = _extract_param(trim1)
    t2 = _extract_param(trim2)
    if t1 is None or t2 is None:
        return []

    # ── Handle sense and sweep direction ──────────────────────────
    if sense:
        if t2 <= t1:
            t2 += 2.0 * math.pi
    else:
        if t1 <= t2:
            t1 += 2.0 * math.pi

    num_points = max(num_points, 2)
    result = []
    for i in range(num_points):
        frac = i / (num_points - 1)
        theta = t1 + frac * (t2 - t1)
        # Point in world coords via local axes
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        x = cx + radius * (cos_t * lx[0] + sin_t * ly[0])
        y = cy + radius * (cos_t * lx[1] + sin_t * ly[1])
        z = cz + radius * (cos_t * lx[2] + sin_t * ly[2])
        result.append((x, y, z))

    return result



def _parse_composite_curve(directrix):
    """Parse an IfcCompositeCurve into a list of (x, y, z) coordinate tuples.

    Iterates over each IfcCompositeCurveSegment and handles:
    - IfcPolyline parent curves (extract points directly)
    - IfcTrimmedCurve with IfcCircle basis (discretize arc)
    - IfcLine parent curves (extract the line origin point)

    Parameters
    ----------
    directrix:
        An IfcCompositeCurve entity.

    Returns
    -------
    list of tuple
        Ordered (x, y, z) coordinate tuples along the composite curve.
    """
    segments = getattr(directrix, "Segments", None) or []
    accumulated = []

    for seg in segments:
        parent = getattr(seg, "ParentCurve", None)
        if parent is None:
            continue

        same_sense = getattr(seg, "SameSense", True)
        seg_pts = []

        if parent.is_a("IfcPolyline"):
            raw_pts = getattr(parent, "Points", []) or []
            for p in raw_pts:
                coords = getattr(p, "Coordinates", None)
                if coords is not None:
                    c = list(coords)
                    while len(c) < 3:
                        c.append(0.0)
                    seg_pts.append(tuple(c))

        elif parent.is_a("IfcTrimmedCurve"):
            basis = getattr(parent, "BasisCurve", None)
            if basis is not None and basis.is_a("IfcCircle"):
                seg_pts = _discretize_trimmed_circle(parent)

        elif parent.is_a("IfcLine"):
            pnt = getattr(parent, "Pnt", None)
            if pnt is not None:
                coords = getattr(pnt, "Coordinates", None)
                if coords is not None:
                    c = list(coords)
                    while len(c) < 3:
                        c.append(0.0)
                    seg_pts.append(tuple(c))

        # Respect SameSense flag
        if not same_sense:
            seg_pts = list(reversed(seg_pts))

        # Deduplicate junction points (skip first point if matches last)
        for pt in seg_pts:
            if accumulated:
                last = accumulated[-1]
                dist_sq = sum((a - b) ** 2 for a, b in zip(last, pt))
                if dist_sq < 1e-12:  # tolerance ~1e-6 per axis
                    continue
            accumulated.append(pt)

    return accumulated


def get_bar_directrix_points(bar: ifcopenshell.entity_instance) -> list[tuple[float, float, ...]]:
    """Extract the local coordinates of the bar's directrix centerline.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity.

    Returns
    -------
    list of tuple of float
        Local Coordinates of rebar path.
    """
    rep = getattr(bar, "Representation", None)
    if not rep:
        return []
    for shape_rep in getattr(rep, "Representations", []):
        for item in getattr(shape_rep, "Items", []):
            if item.is_a("IfcSweptDiskSolid"):
                directrix = getattr(item, "Directrix", None)
                if not directrix:
                    continue
                if directrix.is_a("IfcPolyline"):
                    pts = getattr(directrix, "Points", [])
                    return [tuple(unwrap(p.Coordinates)) for p in pts if getattr(p, "Coordinates", None)]
                elif directrix.is_a("IfcIndexedPolyCurve"):
                    points = getattr(directrix, "Points", None)
                    if points and getattr(points, "CoordList", None):
                        return [tuple(c) for c in points.CoordList]
                elif directrix.is_a("IfcCompositeCurve"):
                    pts = _parse_composite_curve(directrix)
                    if pts:
                        return pts
    return []


def get_bar_bend_info(bar, unit_scale: float = 1.0):
    """Extract bend/hook information from bar's directrix.

    Analyses the bar's IfcSweptDiskSolid directrix and returns structured
    segment data (straight runs and arcs with angle, radius, length).
    Supports IfcIndexedPolyCurve (via IfcArcIndex/IfcLineIndex) and
    IfcCompositeCurve (IfcPolyline / IfcTrimmedCurve-on-IfcCircle
    segments).

    Angle convention (both curve types): ``angle_deg`` is the **true
    included bend angle in [0, 180]** degrees.  Reflex readings are
    folded (``angle = 360 - angle`` when the raw central angle exceeds
    180°), so a 90° hook always reports ~90 — never 270.  Arc length is
    computed from the folded angle (``radius * radians(angle_deg)``).
    ``SenseAgreement=False`` on trimmed-curve segments is honoured per
    the IFC semantics (the segment runs from Trim1 to Trim2 in the
    direction of decreasing parameter) and yields the same included
    angle as the equivalent ``SenseAgreement=True`` arc.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity (IfcReinforcingBar or similar).
    unit_scale:
        Factor that converts the bar's model length units to
        millimetres (see :func:`length_unit_to_mm`).  Defaults to 1.0,
        i.e. a millimetre-authored model.  Pass
        ``length_unit_to_mm(model)`` explicitly for other unit systems.

    Returns
    -------
    list of dict
        Each dict has keys:
        - ``"type"``: ``"straight"`` or ``"arc"``
        - ``"length_mm"``: segment length in **millimetres** (float),
          i.e. model-unit length multiplied by *unit_scale*
        - ``"radius_mm"``: arc radius in **millimetres** (arcs only,
          0 for straight)
        - ``"angle_deg"``: included bend angle in degrees, in [0, 180]
          (arcs only, 0 for straight)
        - ``"length"``: **deprecated** alias — segment length in model
          units (same numeric meaning as before the ``length_mm`` key
          existed)
        - ``"radius"``: **deprecated** alias — arc radius in model units
        Returns ``[]`` if the bar has no parseable bend geometry.
    """

    def _segment(seg_type, length, angle_deg, radius):
        return {
            "type": seg_type,
            "length_mm": length * unit_scale,
            "radius_mm": radius * unit_scale,
            "angle_deg": angle_deg,
            # Deprecated aliases (model units) — kept for backward
            # compatibility; prefer length_mm / radius_mm.
            "length": length,
            "radius": radius,
        }

    rep = getattr(bar, "Representation", None)
    if not rep:
        return []

    # Locate the IfcSweptDiskSolid directrix
    directrix = None
    for shape_rep in getattr(rep, "Representations", []):
        for item in getattr(shape_rep, "Items", []):
            if item.is_a("IfcSweptDiskSolid"):
                directrix = getattr(item, "Directrix", None)
                if directrix is not None:
                    break
        if directrix is not None:
            break

    if directrix is None:
        return []

    segments = []

    # ── IfcIndexedPolyCurve ───────────────────────────────────────
    if directrix.is_a("IfcIndexedPolyCurve"):
        points_obj = getattr(directrix, "Points", None)
        if points_obj is None or not getattr(points_obj, "CoordList", None):
            return []
        coord_list = list(points_obj.CoordList)
        seg_indices = getattr(directrix, "Segments", None) or []

        if not seg_indices:
            # No explicit segments — treat entire coord list as a polyline
            for i in range(len(coord_list) - 1):
                p1 = coord_list[i]
                p2 = coord_list[i + 1]
                length = _vec_dist(p1, p2)
                segments.append(_segment("straight", length, 0.0, 0.0))
            return segments

        for seg_idx in seg_indices:
            # seg_idx is either IfcLineIndex or IfcArcIndex. Read the
            # entity type BEFORE unwrapping — unwrap() discards it, and a
            # 3-point IfcLineIndex is otherwise indistinguishable from an
            # IfcArcIndex (a straight bar would emit a phantom 0° arc).
            seg_type_name = seg_idx.is_a() if hasattr(seg_idx, "is_a") else ""
            idx_val = unwrap(seg_idx)
            if not hasattr(idx_val, '__len__'):
                continue
            indices = list(idx_val)

            if seg_type_name == "IfcArcIndex" or (
                not seg_type_name and len(indices) == 3
            ):
                if len(indices) != 3:
                    continue
                # IfcArcIndex: (start_idx, mid_idx, end_idx) — 1-based
                i0 = int(indices[0]) - 1
                i1 = int(indices[1]) - 1
                i2 = int(indices[2]) - 1
                if not (0 <= i0 < len(coord_list) and
                        0 <= i1 < len(coord_list) and
                        0 <= i2 < len(coord_list)):
                    continue
                p_start = coord_list[i0]
                p_mid = coord_list[i1]
                p_end = coord_list[i2]

                # Inscribed angle theorem (with reflex fold — see
                # _arc_from_three_points)
                angle_deg, radius, arc_len = _arc_from_three_points(
                    p_start, p_mid, p_end
                )
                segments.append(_segment("arc", arc_len, angle_deg, radius))

            elif len(indices) >= 2:
                # IfcLineIndex: chain of straight segments
                for k in range(len(indices) - 1):
                    i_a = int(indices[k]) - 1
                    i_b = int(indices[k + 1]) - 1
                    if not (0 <= i_a < len(coord_list) and
                            0 <= i_b < len(coord_list)):
                        continue
                    length = _vec_dist(coord_list[i_a], coord_list[i_b])
                    segments.append(_segment("straight", length, 0.0, 0.0))

    # ── IfcCompositeCurve ─────────────────────────────────────────
    elif directrix.is_a("IfcCompositeCurve"):
        comp_segments = getattr(directrix, "Segments", None) or []
        for seg in comp_segments:
            parent = getattr(seg, "ParentCurve", None)
            if parent is None:
                continue

            if parent.is_a("IfcPolyline"):
                raw_pts = getattr(parent, "Points", []) or []
                poly_coords = []
                for p in raw_pts:
                    coords = getattr(p, "Coordinates", None)
                    if coords is not None:
                        c = list(coords)
                        while len(c) < 3:
                            c.append(0.0)
                        poly_coords.append(tuple(c))
                for k in range(len(poly_coords) - 1):
                    length = _vec_dist(poly_coords[k], poly_coords[k + 1])
                    segments.append(_segment("straight", length, 0.0, 0.0))

            elif parent.is_a("IfcTrimmedCurve"):
                basis = getattr(parent, "BasisCurve", None)
                if basis is not None and basis.is_a("IfcCircle"):
                    arc_radius = float(unwrap(getattr(basis, "Radius", 0.0)))
                    
                    # Extract center coordinates for Cartesian point trim fallback
                    position = getattr(basis, "Position", None)
                    cx, cy = 0.0, 0.0
                    if position is not None:
                        loc = getattr(position, "Location", None)
                        if loc is not None:
                            coords = getattr(loc, "Coordinates", (0.0, 0.0))
                            if len(coords) >= 2:
                                cx, cy = float(coords[0]), float(coords[1])
                                
                    # Extract angles from trim parameters
                    trim1 = getattr(parent, "Trim1", None) or ()
                    trim2 = getattr(parent, "Trim2", None) or ()
                    model = getattr(parent, "file", None)
                    is_degrees = _plane_angle_unit_is_degrees(model) if model else True
                    
                    t1 = _extract_trim_param(trim1, cx, cy, is_degrees)
                    t2 = _extract_trim_param(trim2, cx, cy, is_degrees)
                    sense = getattr(parent, "SenseAgreement", True)

                    if t1 is not None and t2 is not None:
                        two_pi = 2.0 * math.pi
                        # IFC semantics: SenseAgreement=True → the
                        # trimmed curve runs from Trim1 to Trim2 in the
                        # direction of INCREASING parameter;
                        # SenseAgreement=False → DECREASING parameter.
                        if sense is False:
                            sweep = (t1 - t2) % two_pi
                        else:
                            sweep = (t2 - t1) % two_pi
                        # Fold to the true included bend angle in
                        # [0, 180] — same convention as the
                        # IfcArcIndex branch.
                        if sweep > math.pi:
                            sweep = two_pi - sweep
                        arc_len = arc_radius * sweep
                        segments.append(_segment(
                            "arc", arc_len, math.degrees(sweep), arc_radius
                        ))
                    else:
                        # Fallback: discretize and measure chord
                        arc_pts = _discretize_trimmed_circle(parent)
                        if len(arc_pts) >= 2:
                            arc_len = sum(
                                _vec_dist(arc_pts[j], arc_pts[j + 1])
                                for j in range(len(arc_pts) - 1)
                            )
                            segments.append(_segment(
                                "arc", arc_len, 0.0, arc_radius
                            ))

    return segments


# ── Private helpers for bend-info computation ─────────────────────────

def _vec_dist(p1, p2):
    """Euclidean distance between two coordinate tuples."""
    coords1 = list(p1)
    coords2 = list(p2)
    while len(coords1) < 3:
        coords1.append(0.0)
    while len(coords2) < 3:
        coords2.append(0.0)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(coords1, coords2)))


def _arc_from_three_points(p_start, p_mid, p_end):
    """Compute arc bend angle, radius, and arc length from 3 points.

    Uses the inscribed-angle theorem: the angle at the midpoint between
    vectors to start and end is half the central angle of the arc *not*
    containing the midpoint.  Because the mid point of an IfcArcIndex
    lies ON the traversed arc, the raw ``2 * inscribed`` value is the
    central angle of the OTHER (complementary) arc — reflex for any
    bend sharper than 180°.  The result is therefore folded
    (``angle = 360 - angle`` when > 180) so the returned angle is the
    true included bend angle in [0, 180], and the arc length is
    computed from the folded angle (``radius * radians(angle)``).

    Without the fold a 90° hook reports 270° and a 135° hook reports
    225° (and the arc length is inflated accordingly) — the bug behind
    the 8.1.2.1.B vacuous passes.

    Returns
    -------
    tuple of (angle_deg, radius, arc_length)
        ``angle_deg`` in [0, 180]; ``radius`` and ``arc_length`` in the
        units of the input coordinates (model units).
    """
    s = list(p_start)
    m = list(p_mid)
    e = list(p_end)
    while len(s) < 3:
        s.append(0.0)
    while len(m) < 3:
        m.append(0.0)
    while len(e) < 3:
        e.append(0.0)

    # Vectors from mid to start and mid to end
    v1 = [s[i] - m[i] for i in range(3)]
    v2 = [e[i] - m[i] for i in range(3)]

    dot = sum(a * b for a, b in zip(v1, v2))
    len1 = math.sqrt(sum(a * a for a in v1))
    len2 = math.sqrt(sum(a * a for a in v2))

    if len1 < 1e-12 or len2 < 1e-12:
        # Degenerate: points are coincident
        chord = _vec_dist(s, e)
        return (0.0, 0.0, chord)

    cos_inscribed = max(-1.0, min(1.0, dot / (len1 * len2)))
    inscribed_angle = math.acos(cos_inscribed)

    # Central angle of the arc NOT containing the mid point
    central_angle = 2.0 * inscribed_angle

    # Chord length between start and end
    chord = _vec_dist(s, e)

    # Radius from chord and central angle.  Note sin(θ/2) is invariant
    # under the reflex fold (sin((360-θ)/2) == sin(θ/2)), so the radius
    # may be computed before folding.
    sin_half = math.sin(central_angle / 2.0)
    if abs(sin_half) < 1e-12:
        # Nearly straight — treat as straight segment
        return (0.0, 0.0, chord)

    radius = chord / (2.0 * sin_half)

    # Reflex fold: the IfcArcIndex mid point lies ON the traversed arc,
    # so a raw value above 180° means the complementary arc was
    # measured — the true included bend angle is 360° minus it.
    if central_angle > math.pi:
        central_angle = 2.0 * math.pi - central_angle

    arc_length = radius * central_angle

    return (math.degrees(central_angle), radius, arc_length)


def _extract_trim_param(trim_set, cx=0.0, cy=0.0, is_degrees=True):
    """Extract a parameter angle (radians) from a trim specification."""
    if not trim_set:
        return None
    for val in trim_set:
        raw = unwrap(val)
        if isinstance(raw, (int, float)):
            val_float = float(raw)
            if is_degrees:
                return math.radians(val_float)
            return val_float
    for val in trim_set:
        coords = getattr(val, "Coordinates", None)
        if coords is not None:
            c = list(coords)
            dx = float(c[0]) - cx
            dy = float(c[1]) - cy if len(c) > 1 else 0.0
            return math.atan2(dy, dx)
    return None



def get_bar_bbox_fast(
    bar: ifcopenshell.entity_instance,
    unit_scale: float,
) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
    """Calculate the axis-aligned bounding box of a bar in mm.

    Avoids using create_shape by parsing placement + directrix, falls back to create_shape if parsing fails.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity.
    unit_scale:
        The scale factor from length_unit_to_mm.

    Returns
    -------
    tuple or None
        ((x_min, y_min, z_min), (x_max, y_max, z_max)) in mm.
    """
    local_pts = get_bar_directrix_points(bar)
    placement = getattr(bar, "ObjectPlacement", None)
    
    # Try fast path
    if local_pts and placement:
        import ifcopenshell.util.placement
        import numpy as np
        try:
            matrix = ifcopenshell.util.placement.get_local_placement(placement)
            world_pts = []
            for pt in local_pts:
                pt_3d = list(pt)
                while len(pt_3d) < 3:
                    pt_3d.append(0.0)

                v = np.array([pt_3d[0], pt_3d[1], pt_3d[2], 1.0])
                trans = matrix @ v
                world_pts.append(trans[:3] * unit_scale)

            radius = 0.0
            rep = getattr(bar, "Representation", None)
            if rep:
                for shape_rep in getattr(rep, "Representations", []):
                    for item in getattr(shape_rep, "Items", []):
                        if item.is_a("IfcSweptDiskSolid"):
                            radius = float(unwrap(getattr(item, "Radius", 0.0))) * unit_scale
                            break

            xs = [p[0] for p in world_pts]
            ys = [p[1] for p in world_pts]
            zs = [p[2] for p in world_pts]

            return (
                (min(xs) - radius, min(ys) - radius, min(zs) - radius),
                (max(xs) + radius, max(ys) + radius, max(zs) + radius)
            )
        except Exception:
            pass

    # Fallback path using create_shape
    import ifcopenshell.geom
    try:
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        shape = ifcopenshell.geom.create_shape(settings, bar)
        verts = shape.geometry.verts
        pts = [verts[i : i + 3] for i in range(0, len(verts), 3)]
        if pts:
            xs = [p[0] * 1000.0 for p in pts]
            ys = [p[1] * 1000.0 for p in pts]
            zs = [p[2] * 1000.0 for p in pts]
            
            # Fetch radius/diameter from properties to adjust bounds if possible
            radius = 0.0
            diam = getattr(bar, "NominalDiameter", None)
            if diam:
                radius = float(unwrap(diam)) * unit_scale / 2.0
            
            return (
                (min(xs) - radius, min(ys) - radius, min(zs) - radius),
                (max(xs) + radius, max(ys) + radius, max(zs) + radius)
            )
    except Exception:
        pass
        
    return None


def get_bar_centroid_fast(
    bar: ifcopenshell.entity_instance,
    unit_scale: float,
) -> Optional[Tuple[float, float, float]]:
    """Get the centroid of the rebar in mm without geometry engine.

    Parameters
    ----------
    bar:
        A reinforcing-bar entity.
    unit_scale:
        The scale factor from length_unit_to_mm.

    Returns
    -------
    tuple or None
        (x, y, z) coordinate of the centroid in mm.
    """
    bbox = get_bar_bbox_fast(bar, unit_scale)
    if not bbox:
        return None
    min_pt, max_pt = bbox
    return (
        (min_pt[0] + max_pt[0]) / 2.0,
        (min_pt[1] + max_pt[1]) / 2.0,
        (min_pt[2] + max_pt[2]) / 2.0
    )
