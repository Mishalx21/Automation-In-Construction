"""Programmatic synthetic IFC builders (tests + insertion operators).

Every builder constructs entities in-memory with ``ifcopenshell.file``
(IFC4 schema) — no template files on disk.  Geometry builders return
``(bar, truth)`` tuples where *truth* is a dict of the exact constructed
parameters (bend angle, bend radius, lead-in and tail lengths, all in
**model units**) so callers can assert against known ground truth.

Dual use by design: the ifc_helpers test suite builds micro-models from
these, and ``bnbc.fixtures.operators.insert`` grafts the same geometry into
fixture models — one tested implementation of hook geometry, no drift.

Coordinate convention for hook builders
---------------------------------------
All hooks are drawn in the XY plane:

* a straight *lead-in* along +X from ``(0, 0)`` to ``(lead_in, 0)``;
* a circular bend of radius ``bend_radius`` centred at
  ``(lead_in, bend_radius)`` swept counter-clockwise by
  ``bend_angle_deg`` starting from the parameter angle ``-90°`` (which
  is exactly the lead-in end point, with a tangent along +X);
* a straight *tail* of length ``tail`` along the tangent at the arc
  end point.

The included bend angle between the two straight legs therefore equals
the arc sweep, and the tail chord length equals ``tail`` exactly.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ifcopenshell


def _guid() -> str:
    return ifcopenshell.guid.new()


# ─────────────────────────────────────────────────────────────────────
# Project / units / context scaffold
# ─────────────────────────────────────────────────────────────────────
def make_model(
    units: Optional[str] = "mm",
    angle_unit: Optional[str] = "radian",
    with_project: bool = True,
    with_units: bool = True,
) -> Tuple[ifcopenshell.file, Any]:
    """Create a minimal IFC4 model scaffold.

    Parameters
    ----------
    units:
        ``"mm"`` (millimetre SI unit), ``"m"`` (metre SI unit),
        ``"cm"`` (centimetre SI unit), ``"inch"`` (conversion-based
        unit, 0.0254 m per unit), or ``None`` for *no* length unit.
    angle_unit:
        ``"radian"`` (SI), ``"degree"`` (conversion-based), or ``None``.
    with_project:
        When False, no IfcProject is created at all.
    with_units:
        When False, the IfcProject is created with
        ``UnitsInContext=None``.

    Returns
    -------
    (model, context)
        The file handle and an ``IfcGeometricRepresentationContext``
        usable for shape representations.
    """
    model = ifcopenshell.file(schema="IFC4")

    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    placement = model.create_entity("IfcAxis2Placement3D", Location=origin)
    context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=placement,
    )

    if not with_project:
        return model, context

    unit_assignment = None
    if with_units:
        unit_entities: List[Any] = []

        if units == "mm":
            unit_entities.append(model.create_entity(
                "IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE"
            ))
        elif units == "m":
            unit_entities.append(model.create_entity(
                "IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE"
            ))
        elif units == "cm":
            unit_entities.append(model.create_entity(
                "IfcSIUnit", UnitType="LENGTHUNIT", Prefix="CENTI", Name="METRE"
            ))
        elif units == "inch":
            metre = model.create_entity(
                "IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE"
            )
            measure = model.create_entity(
                "IfcMeasureWithUnit",
                ValueComponent=model.create_entity("IfcLengthMeasure", 0.0254),
                UnitComponent=metre,
            )
            dims = model.create_entity(
                "IfcDimensionalExponents", 1, 0, 0, 0, 0, 0, 0
            )
            unit_entities.append(model.create_entity(
                "IfcConversionBasedUnit",
                Dimensions=dims,
                UnitType="LENGTHUNIT",
                Name="INCH",
                ConversionFactor=measure,
            ))
        elif units is None:
            pass
        else:  # pragma: no cover - guard against typos in tests
            raise ValueError(f"unknown units spec: {units!r}")

        if angle_unit == "radian":
            unit_entities.append(model.create_entity(
                "IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN"
            ))
        elif angle_unit == "degree":
            radian = model.create_entity(
                "IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN"
            )
            measure = model.create_entity(
                "IfcMeasureWithUnit",
                ValueComponent=model.create_entity(
                    "IfcPlaneAngleMeasure", math.pi / 180.0
                ),
                UnitComponent=radian,
            )
            dims = model.create_entity(
                "IfcDimensionalExponents", 0, 0, 0, 0, 0, 0, 0
            )
            unit_entities.append(model.create_entity(
                "IfcConversionBasedUnit",
                Dimensions=dims,
                UnitType="PLANEANGLEUNIT",
                Name="DEGREE",
                ConversionFactor=measure,
            ))

        unit_assignment = model.create_entity(
            "IfcUnitAssignment", Units=unit_entities
        )

    model.create_entity(
        "IfcProject",
        GlobalId=_guid(),
        Name="TestProject",
        UnitsInContext=unit_assignment,
    )
    return model, context


# ─────────────────────────────────────────────────────────────────────
# Spatial structure
# ─────────────────────────────────────────────────────────────────────
def add_storey(
    model: ifcopenshell.file,
    name: Optional[str] = "Level 1",
    long_name: Optional[str] = None,
) -> Any:
    """Create an IfcBuildingStorey (aggregated under project→site→building
    when an IfcProject exists)."""
    storey = model.create_entity(
        "IfcBuildingStorey", GlobalId=_guid(), Name=name, LongName=long_name
    )
    projects = model.by_type("IfcProject")
    if projects:
        site = model.create_entity("IfcSite", GlobalId=_guid(), Name="Site")
        building = model.create_entity(
            "IfcBuilding", GlobalId=_guid(), Name="Building"
        )
        aggregate(model, projects[0], [site])
        aggregate(model, site, [building])
        aggregate(model, building, [storey])
    return storey


def contain_in_structure(
    model: ifcopenshell.file, structure: Any, elements: Sequence[Any]
) -> Any:
    """Relate *elements* to *structure* via IfcRelContainedInSpatialStructure."""
    return model.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_guid(),
        RelatingStructure=structure,
        RelatedElements=list(elements),
    )


def aggregate(
    model: ifcopenshell.file, parent: Any, children: Sequence[Any]
) -> Any:
    """Relate *children* to *parent* via IfcRelAggregates."""
    return model.create_entity(
        "IfcRelAggregates",
        GlobalId=_guid(),
        RelatingObject=parent,
        RelatedObjects=list(children),
    )


# ─────────────────────────────────────────────────────────────────────
# Property sets
# ─────────────────────────────────────────────────────────────────────
def add_pset(
    model: ifcopenshell.file,
    element: Any,
    pset_name: str,
    props: Dict[str, Any],
) -> Any:
    """Attach an IfcPropertySet of IfcPropertySingleValue entries."""
    prop_entities = []
    for key, value in props.items():
        if isinstance(value, bool):
            nominal = model.create_entity("IfcBoolean", value)
        elif isinstance(value, int):
            nominal = model.create_entity("IfcInteger", value)
        elif isinstance(value, float):
            nominal = model.create_entity("IfcReal", value)
        else:
            nominal = model.create_entity("IfcLabel", str(value))
        prop_entities.append(model.create_entity(
            "IfcPropertySingleValue", Name=key, NominalValue=nominal
        ))
    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=_guid(),
        Name=pset_name,
        HasProperties=prop_entities,
    )
    return model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_guid(),
        RelatedObjects=[element],
        RelatingPropertyDefinition=pset,
    )


# ─────────────────────────────────────────────────────────────────────
# Host elements (beams / columns / walls / slabs)
# ─────────────────────────────────────────────────────────────────────
def make_host_element(
    model: ifcopenshell.file,
    context: Any,
    ifc_class: str = "IfcBeam",
    length: float = 3000.0,
    width: float = 300.0,
    height: float = 500.0,
    name: str = "Host",
    predefined_type: Optional[str] = None,
) -> Tuple[Any, Dict[str, float]]:
    """A host element with a real extruded-rectangle body and a placement.

    Orientation conventions (world axes, element at the origin). ``length``
    is ALWAYS the member's axis extent and ``width`` x ``height`` its
    cross-section — the natural reading spec models use unprompted:

    * ``IfcBeam`` / ``IfcWall`` / generic — axis along +X (``length``),
      cross-section ``width`` in Y and ``height`` in Z, centred on the X axis;
    * ``IfcColumn`` — axis VERTICAL (+Z, ``length``), cross-section
      ``width`` (X) x ``height`` (Y);
    * ``IfcSlab`` — plan ``length`` (X) x ``width`` (Y), extruded ``height``
      (thickness) in Z.

    Returns ``(element, truth)`` where truth carries the exact world-axis
    extents ``{"dx": ..., "dy": ..., "dz": ...}``.
    """
    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))

    if ifc_class == "IfcSlab":
        # Plan-profile, extruded up by thickness.
        profile = model.create_entity(
            "IfcRectangleProfileDef", ProfileType="AREA",
            XDim=float(length), YDim=float(width),
        )
        position = model.create_entity("IfcAxis2Placement3D", Location=origin)
        depth = float(height)
        extents = {"dx": float(length), "dy": float(width), "dz": float(height)}
    elif ifc_class == "IfcColumn":
        # Cross-section (width x height) in plan, extruded up by LENGTH —
        # length is the axis extent for every member class (observed live:
        # the spec model authored columns beam-style and the old
        # height-extruded mapping built 3-metre-wide slabs, poisoning every
        # expected verdict on 8.3.5.1).
        profile = model.create_entity(
            "IfcRectangleProfileDef", ProfileType="AREA",
            XDim=float(width), YDim=float(height),
        )
        position = model.create_entity("IfcAxis2Placement3D", Location=origin)
        depth = float(length)
        extents = {"dx": float(width), "dy": float(height), "dz": float(length)}
    else:
        # Axis along +X: local Z (extrusion) -> world X, local X -> world Y,
        # local Y -> world Z. Profile XDim = width (Y), YDim = height (Z).
        profile = model.create_entity(
            "IfcRectangleProfileDef", ProfileType="AREA",
            XDim=float(width), YDim=float(height),
        )
        position = model.create_entity(
            "IfcAxis2Placement3D",
            Location=origin,
            Axis=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=(0.0, 1.0, 0.0)),
        )
        depth = float(length)
        extents = {"dx": float(length), "dy": float(width), "dz": float(height)}

    solid = model.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=position,
        ExtrudedDirection=model.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        ),
        Depth=depth,
    )
    shape_rep = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=[solid],
    )
    product_shape = model.create_entity(
        "IfcProductDefinitionShape", Representations=[shape_rep]
    )
    placement = model.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)
            ),
        ),
    )
    kwargs: Dict[str, Any] = {
        "GlobalId": _guid(),
        "Name": name,
        "ObjectPlacement": placement,
        "Representation": product_shape,
    }
    if predefined_type is not None:
        kwargs["PredefinedType"] = predefined_type
    element = model.create_entity(ifc_class, **kwargs)
    return element, extents


# ─────────────────────────────────────────────────────────────────────
# Reinforcing bars
# ─────────────────────────────────────────────────────────────────────
def make_bare_bar(
    model: ifcopenshell.file,
    name: Optional[str] = None,
    object_type: Optional[str] = None,
    nominal_diameter: Optional[float] = None,
) -> Any:
    """An IfcReinforcingBar with no geometry — for identification tests."""
    kwargs: Dict[str, Any] = {"GlobalId": _guid()}
    if name is not None:
        kwargs["Name"] = name
    if object_type is not None:
        kwargs["ObjectType"] = object_type
    if nominal_diameter is not None:
        kwargs["NominalDiameter"] = float(nominal_diameter)
    return model.create_entity("IfcReinforcingBar", **kwargs)


def _bar_from_directrix(
    model: ifcopenshell.file,
    context: Any,
    directrix: Any,
    name: str = "Bar",
    disk_radius: float = 8.0,
    nominal_diameter: Optional[float] = None,
    object_type: Optional[str] = None,
) -> Any:
    solid = model.create_entity(
        "IfcSweptDiskSolid", Directrix=directrix, Radius=disk_radius
    )
    shape_rep = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="AdvancedSweptSolid",
        Items=[solid],
    )
    product_shape = model.create_entity(
        "IfcProductDefinitionShape", Representations=[shape_rep]
    )
    kwargs: Dict[str, Any] = {
        "GlobalId": _guid(),
        "Name": name,
        "Representation": product_shape,
    }
    if nominal_diameter is not None:
        kwargs["NominalDiameter"] = float(nominal_diameter)
    if object_type is not None:
        kwargs["ObjectType"] = object_type
    return model.create_entity("IfcReinforcingBar", **kwargs)


def hook_geometry(
    bend_angle_deg: float,
    lead_in: float,
    tail: float,
    bend_radius: float,
    scale: float = 1.0,
) -> Dict[str, Any]:
    """Compute the exact hook geometry described in the module docstring.

    All output coordinates/lengths are multiplied by *scale* (use
    ``scale=0.001`` to author the same physical bar in metres).
    """
    sweep = math.radians(bend_angle_deg)
    start_ang = -math.pi / 2.0
    mid_ang = start_ang + sweep / 2.0
    end_ang = start_ang + sweep

    center = (lead_in, bend_radius)

    def on_arc(ang: float) -> Tuple[float, float]:
        return (
            center[0] + bend_radius * math.cos(ang),
            center[1] + bend_radius * math.sin(ang),
        )

    p0 = (0.0, 0.0)
    p1 = (lead_in, 0.0)          # == on_arc(start_ang)
    p_mid = on_arc(mid_ang)
    p2 = on_arc(end_ang)
    # CCW tangent at the arc end
    tangent = (-math.sin(end_ang), math.cos(end_ang))
    p3 = (p2[0] + tail * tangent[0], p2[1] + tail * tangent[1])

    def s(pt: Tuple[float, float]) -> Tuple[float, float]:
        return (pt[0] * scale, pt[1] * scale)

    return {
        "points": [s(p0), s(p1), s(p_mid), s(p2), s(p3)],
        "center": s(center),
        "start_param": start_ang,
        "end_param": end_ang,
        "truth": {
            "bend_angle_deg": bend_angle_deg,
            "bend_radius": bend_radius * scale,
            "arc_length": bend_radius * sweep * scale,
            "lead_in": lead_in * scale,
            "tail": tail * scale,
        },
    }


def make_hook_bar_indexed(
    model: ifcopenshell.file,
    context: Any,
    bend_angle_deg: float = 90.0,
    lead_in: float = 200.0,
    tail: float = 100.0,
    bend_radius: float = 48.0,
    scale: float = 1.0,
    name: str = "Hooked bar",
    nominal_diameter: Optional[float] = 16.0,
) -> Tuple[Any, Dict[str, float]]:
    """A hooked bar whose directrix is an IfcIndexedPolyCurve.

    Segments: IfcLineIndex(1,2) lead-in, IfcArcIndex(2,3,4) bend,
    IfcLineIndex(4,5) tail.  Returns ``(bar, truth)``.
    """
    geo = hook_geometry(bend_angle_deg, lead_in, tail, bend_radius, scale)
    coords = [(float(x), float(y)) for x, y in geo["points"]]
    point_list = model.create_entity(
        "IfcCartesianPointList2D", CoordList=coords
    )
    segments = [
        model.create_entity("IfcLineIndex", (1, 2)),
        model.create_entity("IfcArcIndex", (2, 3, 4)),
        model.create_entity("IfcLineIndex", (4, 5)),
    ]
    directrix = model.create_entity(
        "IfcIndexedPolyCurve",
        Points=point_list,
        Segments=segments,
        SelfIntersect=False,
    )
    bar = _bar_from_directrix(
        model, context, directrix,
        name=name, nominal_diameter=nominal_diameter,
    )
    return bar, geo["truth"]


def make_hook_bar_composite(
    model: ifcopenshell.file,
    context: Any,
    bend_angle_deg: float = 90.0,
    lead_in: float = 200.0,
    tail: float = 100.0,
    bend_radius: float = 48.0,
    scale: float = 1.0,
    sense_agreement: bool = True,
    trim_in_degrees: bool = False,
    name: str = "Hooked bar (composite)",
    nominal_diameter: Optional[float] = 16.0,
) -> Tuple[Any, Dict[str, float]]:
    """The same hook authored as an IfcCompositeCurve.

    Segments: IfcPolyline lead-in, IfcTrimmedCurve on an IfcCircle for
    the bend, IfcPolyline tail.

    When ``sense_agreement`` is False the trim parameters are swapped
    (Trim1 = end parameter, Trim2 = start parameter) so that, per the
    IFC semantics (SenseAgreement=False ⇒ the segment runs from Trim1
    to Trim2 in the direction of DECREASING parameter), the same
    physical arc is described; the composite segment's ``SameSense`` is
    set False accordingly.  The true included bend angle is identical
    in both encodings.

    When ``trim_in_degrees`` is True the trim parameter values are
    written in degrees — pair this with a model whose PLANEANGLEUNIT is
    a degree conversion unit (``make_model(angle_unit="degree")``).

    Returns ``(bar, truth)``.
    """
    geo = hook_geometry(bend_angle_deg, lead_in, tail, bend_radius, scale)
    p0, p1, _p_mid, p2, p3 = geo["points"]

    def cpt(pt: Tuple[float, float]) -> Any:
        return model.create_entity(
            "IfcCartesianPoint", Coordinates=(float(pt[0]), float(pt[1]))
        )

    # Lead-in polyline
    poly_in = model.create_entity("IfcPolyline", Points=[cpt(p0), cpt(p1)])

    # Bend: trimmed circle
    circle = model.create_entity(
        "IfcCircle",
        Position=model.create_entity(
            "IfcAxis2Placement2D", Location=cpt(geo["center"])
        ),
        Radius=float(bend_radius * scale),
    )
    start_param = geo["start_param"]
    end_param = geo["end_param"]
    if trim_in_degrees:
        start_param = math.degrees(start_param)
        end_param = math.degrees(end_param)
    if sense_agreement:
        trim1_val, trim2_val = start_param, end_param
    else:
        trim1_val, trim2_val = end_param, start_param
    trimmed = model.create_entity(
        "IfcTrimmedCurve",
        BasisCurve=circle,
        Trim1=[model.create_entity("IfcParameterValue", float(trim1_val))],
        Trim2=[model.create_entity("IfcParameterValue", float(trim2_val))],
        SenseAgreement=bool(sense_agreement),
        MasterRepresentation="PARAMETER",
    )

    # Tail polyline
    poly_out = model.create_entity("IfcPolyline", Points=[cpt(p2), cpt(p3)])

    segments = [
        model.create_entity(
            "IfcCompositeCurveSegment",
            Transition="CONTINUOUS", SameSense=True, ParentCurve=poly_in,
        ),
        model.create_entity(
            "IfcCompositeCurveSegment",
            Transition="CONTINUOUS",
            SameSense=bool(sense_agreement),
            ParentCurve=trimmed,
        ),
        model.create_entity(
            "IfcCompositeCurveSegment",
            Transition="DISCONTINUOUS", SameSense=True, ParentCurve=poly_out,
        ),
    ]
    directrix = model.create_entity(
        "IfcCompositeCurve", Segments=segments, SelfIntersect=False
    )
    bar = _bar_from_directrix(
        model, context, directrix,
        name=name, nominal_diameter=nominal_diameter,
    )
    return bar, geo["truth"]


def make_straight_bar(
    model: ifcopenshell.file,
    context: Any,
    length: float = 1000.0,
    name: str = "Straight bar",
    nominal_diameter: Optional[float] = 16.0,
) -> Tuple[Any, Dict[str, float]]:
    """A straight bar (single IfcLineIndex chain over three points)."""
    half = length / 2.0
    point_list = model.create_entity(
        "IfcCartesianPointList2D",
        CoordList=[(0.0, 0.0), (half, 0.0), (length, 0.0)],
    )
    segments = [model.create_entity("IfcLineIndex", (1, 2, 3))]
    directrix = model.create_entity(
        "IfcIndexedPolyCurve",
        Points=point_list,
        Segments=segments,
        SelfIntersect=False,
    )
    bar = _bar_from_directrix(
        model, context, directrix,
        name=name, nominal_diameter=nominal_diameter,
    )
    return bar, {"length": length}


def make_closed_stirrup_bar(
    model: ifcopenshell.file,
    context: Any,
    width: float = 200.0,
    height: float = 300.0,
    corner_radius: float = 20.0,
    name: str = "Stirrup",
    nominal_diameter: Optional[float] = 8.0,
) -> Tuple[Any, Dict[str, float]]:
    """A closed rectangular stirrup: 4 straight edges + 4 x 90° corner arcs.

    Authored as an IfcIndexedPolyCurve traversed counter-clockwise
    starting at the bottom-left corner-arc end point ``(r, 0)``.
    Returns ``(bar, truth)`` where truth carries the exact edge lengths
    and corner parameters.
    """
    w, h, r = width, height, corner_radius
    k = r / math.sqrt(2.0)

    coords = [
        (r, 0.0),                       # 1  bottom edge start
        (w - r, 0.0),                   # 2  bottom edge end / arc1 start
        (w - r + k, r - k),             # 3  arc1 mid (center (w-r, r))
        (w, r),                         # 4  arc1 end / right edge start
        (w, h - r),                     # 5  right edge end / arc2 start
        (w - r + k, h - r + k),         # 6  arc2 mid (center (w-r, h-r))
        (w - r, h),                     # 7  arc2 end / top edge start
        (r, h),                         # 8  top edge end / arc3 start
        (r - k, h - r + k),             # 9  arc3 mid (center (r, h-r))
        (0.0, h - r),                   # 10 arc3 end / left edge start
        (0.0, r),                       # 11 left edge end / arc4 start
        (r - k, r - k),                 # 12 arc4 mid (center (r, r))
    ]
    point_list = model.create_entity(
        "IfcCartesianPointList2D",
        CoordList=[(float(x), float(y)) for x, y in coords],
    )
    segments = [
        model.create_entity("IfcLineIndex", (1, 2)),
        model.create_entity("IfcArcIndex", (2, 3, 4)),
        model.create_entity("IfcLineIndex", (4, 5)),
        model.create_entity("IfcArcIndex", (5, 6, 7)),
        model.create_entity("IfcLineIndex", (7, 8)),
        model.create_entity("IfcArcIndex", (8, 9, 10)),
        model.create_entity("IfcLineIndex", (10, 11)),
        model.create_entity("IfcArcIndex", (11, 12, 1)),
    ]
    directrix = model.create_entity(
        "IfcIndexedPolyCurve",
        Points=point_list,
        Segments=segments,
        SelfIntersect=False,
    )
    bar = _bar_from_directrix(
        model, context, directrix,
        name=name, nominal_diameter=nominal_diameter,
    )
    truth = {
        "corner_angle_deg": 90.0,
        "corner_radius": r,
        "corner_arc_length": r * math.pi / 2.0,
        "edge_lengths": sorted([w - 2 * r, w - 2 * r, h - 2 * r, h - 2 * r]),
        "n_arcs": 4,
        "n_straights": 4,
    }
    return bar, truth
