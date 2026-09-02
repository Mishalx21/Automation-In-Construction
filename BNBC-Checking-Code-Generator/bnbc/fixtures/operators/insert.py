"""Insertion operators: add a synthetic feature instance to a model.

This is the answer to "the real corpus cannot exercise this rule" (the
8.1.2.1 hook problem): when no bar in the corpus has a hook, the fixture
*inserts* one with exactly known geometry, so the checker has something to
measure — and the expected verdict is manufactured, not guessed.

Geometry comes from ``bnbc.fixtures.synthetic`` — the same builders the
``ifc_helpers`` test suite asserts against, so the inserted truth values are
themselves under test.
"""

from __future__ import annotations

from typing import Optional

from bnbc.fixtures import measure, synthetic
from bnbc.fixtures.errors import PlanningError, UnsupportedGeometryError
from bnbc.fixtures.operators.base import Operator, OperatorResult, register


def _dia_text(nominal_diameter_mm) -> str:
    return f"{nominal_diameter_mm:.1f} mm" if nominal_diameter_mm is not None else "absent"


def _contain_in_storey(model, bar, storey_name: Optional[str]) -> None:
    storey = measure.find_storey(model, storey_name) if storey_name else None
    if storey is None:
        storeys = model.by_type("IfcBuildingStorey")
        storey = storeys[0] if storeys else None
    if storey is not None:
        synthetic.contain_in_structure(model, storey, [bar])


def _aggregate_into_host(model, bar, host_name_contains: Optional[str]) -> Optional[str]:
    """Relate the inserted bar to a host element via IfcRelAggregates.

    Relationship-based bar->host association is what containment-driven
    checkers look for first; without it a synthetic scene only works for
    checkers with a geometric fallback. Returns the host GlobalId, or raises
    when the named host does not exist (a plan defect, not a silent skip).
    """
    if not host_name_contains:
        return None
    needle = str(host_name_contains).lower()
    hosts = [
        el for el in model.by_type("IfcElement")
        if not el.is_a("IfcReinforcingElement")
        and needle in str(getattr(el, "Name", "") or "").lower()
    ]
    if not hosts:
        raise PlanningError(
            f"host_name_contains {host_name_contains!r} matches no host element "
            "— insert the host (insert_host_element) in an EARLIER step of the "
            "same sketch, or drop the parameter"
        )
    synthetic.aggregate(model, hosts[0], [bar])
    return hosts[0].GlobalId


def _diameter_expectation(guid: str, nominal_diameter_mm) -> dict:
    if nominal_diameter_mm is not None:
        return {"check": "diameter", "guid": guid,
                "value_mm": float(nominal_diameter_mm), "tol_mm": 0.01}
    return {"check": "attribute", "guid": guid, "name": "NominalDiameter", "value": None}


def _model_context(model):
    """The model's 3D 'Model' representation context (or the first context)."""
    contexts = model.by_type("IfcGeometricRepresentationContext")
    for ctx in contexts:
        # Skip subcontexts; prefer the root Model context.
        if ctx.is_a() == "IfcGeometricRepresentationContext" and \
                (getattr(ctx, "ContextType", None) or "Model") == "Model":
            return ctx
    return contexts[0] if contexts else None


@register
class InsertHostElement(Operator):
    """Insert a host element (IfcBeam/IfcColumn/IfcWall/IfcSlab):
    length_mm = the member's AXIS extent (along X for beam/wall, VERTICAL
    for column), width_mm x height_mm = its cross-section; for IfcSlab,
    length_mm x width_mm is the plan and height_mm the thickness.

    Dimensions are physical millimetres; the body is a real
    extruded-rectangle solid with an ObjectPlacement — the scene scaffold
    for synthetic fixtures. Later steps of the same sketch insert bars into
    it via ``host_name_contains`` (matching this element's ``name``).
    """

    name = "insert_host_element"
    PARAM_DOMAINS = {
        "length_mm": (1.0, None),
        "width_mm": (1.0, None),
        "height_mm": (1.0, None),
    }

    _ALLOWED = ("IfcBeam", "IfcColumn", "IfcWall", "IfcSlab")

    def _apply(self, model, target_selector, *, ifc_class: str = "IfcBeam",
               length_mm: float = 3000.0, width_mm: float = 300.0,
               height_mm: float = 500.0, name: str = "FIV host",
               predefined_type: Optional[str] = None,
               storey_name: Optional[str] = None, **_ignored) -> OperatorResult:
        if ifc_class not in self._ALLOWED:
            raise PlanningError(
                f"insert_host_element supports {', '.join(self._ALLOWED)} "
                f"(got {ifc_class!r})"
            )
        context = _model_context(model)
        if context is None:
            raise UnsupportedGeometryError("model has no IfcGeometricRepresentationContext")

        unit_scale = measure.unit_scale_mm(model)
        to_units = 1.0 / unit_scale
        element, extents = synthetic.make_host_element(
            model, context,
            ifc_class=ifc_class,
            length=float(length_mm) * to_units,
            width=float(width_mm) * to_units,
            height=float(height_mm) * to_units,
            name=name,
            predefined_type=predefined_type,
        )
        _contain_in_storey(model, element, storey_name)

        # Profile dims in mm as authored (world extents in the truth dict are
        # model units; the self-check re-measures in mm).
        if ifc_class == "IfcSlab":
            profile_x, profile_y = float(length_mm), float(width_mm)
        else:
            # Beam/wall/column: the profile IS the cross-section.
            profile_x, profile_y = float(width_mm), float(height_mm)

        claim = (
            f"inserted {ifc_class} {element.GlobalId} '{name}': "
            f"{length_mm:.0f} x {width_mm:.0f} x {height_mm:.0f} mm"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={
                "ifc_class": ifc_class, "length_mm": float(length_mm),
                "width_mm": float(width_mm), "height_mm": float(height_mm),
                "predefined_type": predefined_type,
            },
            affected_guids=[element.GlobalId],
            claim=claim,
            expected=[
                {"check": "guid_present", "guid": element.GlobalId, "ifc_class": ifc_class},
                {"check": "profile_dims", "guid": element.GlobalId,
                 "x_mm": profile_x, "y_mm": profile_y, "tol_mm": 0.5},
            ],
        )


@register
class InsertHookedBar(Operator):
    """Insert a reinforcing bar with an exactly-known terminal hook.

    Parameters are physical millimetres regardless of the model's length
    unit. ``curve_style`` picks the directrix idiom: ``"indexed"``
    (IfcIndexedPolyCurve, IFC4) or ``"composite"`` (IfcCompositeCurve, the
    Revit/IFC2X3 idiom); ``"auto"`` uses indexed on IFC4+ and composite
    otherwise. The bar is contained in ``storey_name`` (or the first storey).
    """

    name = "insert_hooked_bar"
    PARAM_DOMAINS = {
        "bend_angle_deg": (15.0, 225.0),
        "tail_mm": (0.001, None),
        "lead_in_mm": (0.001, None),
        "bend_radius_mm": (0.001, None),
    }
    DOMAIN_NOTE = "for a bar WITHOUT a hook use insert_straight_bar(length_mm=..., nominal_diameter_mm=...)"

    def _apply(self, model, target_selector, *, bend_angle_deg: float = 90.0,
               lead_in_mm: float = 300.0, tail_mm: float = 100.0,
               bend_radius_mm: float = 48.0,
               nominal_diameter_mm: Optional[float] = 16.0,
               bar_name: str = "FIV inserted hooked bar",
               curve_style: str = "auto", storey_name: Optional[str] = None,
               host_name_contains: Optional[str] = None,
               **_ignored) -> OperatorResult:
        # Degenerate hook params are a PLAN defect, not an engine failure:
        # angle/tail/lead-in of ~0 means "a bar with no hook", which is what
        # insert_straight_bar is for (observed live: a spec card requested
        # bend_angle_deg=0 and the fixture then failed self-verification).
        if float(bend_angle_deg) < 15.0 or float(tail_mm) <= 0.0 or float(lead_in_mm) <= 0.0:
            raise PlanningError(
                "insert_hooked_bar requires bend_angle_deg >= 15, tail_mm > 0 and "
                f"lead_in_mm > 0 (got bend_angle_deg={bend_angle_deg}, "
                f"tail_mm={tail_mm}, lead_in_mm={lead_in_mm}); for a bar WITHOUT "
                "a hook use insert_straight_bar(length_mm=..., nominal_diameter_mm=...)"
            )
        context = _model_context(model)
        if context is None:
            raise UnsupportedGeometryError("model has no IfcGeometricRepresentationContext")

        schema = (getattr(model, "schema", "") or "").upper()
        if curve_style == "auto":
            curve_style = "composite" if schema.startswith("IFC2X3") else "indexed"

        unit_scale = measure.unit_scale_mm(model)  # mm per model unit
        to_units = 1.0 / unit_scale                # builder scale: mm -> model units

        # nominal_diameter_mm=None inserts a diameter-less bar (the synthetic
        # "unknown: diameter missing" fixture).
        common = dict(
            bend_angle_deg=float(bend_angle_deg),
            lead_in=float(lead_in_mm),
            tail=float(tail_mm),
            bend_radius=float(bend_radius_mm),
            scale=to_units,
            name=bar_name,
            nominal_diameter=(
                float(nominal_diameter_mm) * to_units
                if nominal_diameter_mm is not None else None
            ),
        )
        if curve_style == "indexed":
            bar, _truth = synthetic.make_hook_bar_indexed(model, context, **common)
        elif curve_style == "composite":
            # Trim parameters must be authored in the MODEL's plane-angle unit
            # (Revit exports use degrees; the builder defaults to radians).
            degree_units = abs(measure.plane_angle_scale_deg(model) - 1.0) < 1e-9
            bar, _truth = synthetic.make_hook_bar_composite(
                model, context, trim_in_degrees=degree_units, **common
            )
        else:
            raise UnsupportedGeometryError(f"unknown curve_style {curve_style!r}")

        _contain_in_storey(model, bar, storey_name)
        _aggregate_into_host(model, bar, host_name_contains)

        claim = (
            f"inserted {curve_style} hooked bar {bar.GlobalId}: "
            f"{bend_angle_deg:.0f} deg bend, tail {tail_mm:.1f} mm, "
            f"radius {bend_radius_mm:.1f} mm, dia {_dia_text(nominal_diameter_mm)}"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={
                "bend_angle_deg": float(bend_angle_deg), "lead_in_mm": float(lead_in_mm),
                "tail_mm": float(tail_mm), "bend_radius_mm": float(bend_radius_mm),
                "nominal_diameter_mm": (
                    float(nominal_diameter_mm) if nominal_diameter_mm is not None else None
                ),
                "curve_style": curve_style,
            },
            affected_guids=[bar.GlobalId],
            claim=claim,
            expected=[
                {"check": "guid_present", "guid": bar.GlobalId, "ifc_class": "IfcReinforcingBar"},
                {
                    "check": "hook", "guid": bar.GlobalId, "end": "end",
                    "angle_deg": float(bend_angle_deg), "tol_deg": 1.0,
                    "tail_mm": float(tail_mm), "radius_mm": float(bend_radius_mm),
                    "tol_mm": 0.5,
                },
                _diameter_expectation(bar.GlobalId, nominal_diameter_mm),
            ],
        )


@register
class InsertStraightBar(Operator):
    """Insert a straight reinforcing bar (no hooks) with known length/diameter.

    The natural fixture for reinforcement-amount/ratio/spacing conditions and
    the "bar without a required hook" violation. IfcIndexedPolyCurve idiom.
    """

    name = "insert_straight_bar"
    PARAM_DOMAINS = {"length_mm": (0.001, None)}

    def _apply(self, model, target_selector, *, length_mm: float = 1000.0,
               nominal_diameter_mm: Optional[float] = 16.0,
               bar_name: str = "FIV inserted straight bar",
               storey_name: Optional[str] = None,
               host_name_contains: Optional[str] = None, **_ignored) -> OperatorResult:
        if float(length_mm) <= 0.0:
            raise PlanningError(f"insert_straight_bar requires length_mm > 0 (got {length_mm})")
        context = _model_context(model)
        if context is None:
            raise UnsupportedGeometryError("model has no IfcGeometricRepresentationContext")

        unit_scale = measure.unit_scale_mm(model)
        to_units = 1.0 / unit_scale
        bar, _truth = synthetic.make_straight_bar(
            model, context,
            length=float(length_mm) * to_units,
            name=bar_name,
            nominal_diameter=(
                float(nominal_diameter_mm) * to_units
                if nominal_diameter_mm is not None else None
            ),
        )
        _contain_in_storey(model, bar, storey_name)
        _aggregate_into_host(model, bar, host_name_contains)

        claim = (
            f"inserted straight bar {bar.GlobalId}: length {length_mm:.1f} mm, "
            f"dia {_dia_text(nominal_diameter_mm)}"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={
                "length_mm": float(length_mm),
                "nominal_diameter_mm": (
                    float(nominal_diameter_mm) if nominal_diameter_mm is not None else None
                ),
            },
            affected_guids=[bar.GlobalId],
            claim=claim,
            expected=[
                {"check": "guid_present", "guid": bar.GlobalId, "ifc_class": "IfcReinforcingBar"},
                # Straight bar: neither end may read as a terminal hook.
                {"check": "hook_absent", "guid": bar.GlobalId},
                _diameter_expectation(bar.GlobalId, nominal_diameter_mm),
            ],
        )


@register
class InsertClosedStirrup(Operator):
    """Insert a closed rectangular stirrup (4 straights + 4 x 90-deg corner arcs).

    The stirrup is a CLOSED loop — its corner bends are not free-end hooks,
    so it is the natural "must NOT be flagged" pass fixture for hook rules.
    IfcIndexedPolyCurve idiom (IFC4 schema only — use a synthetic base).
    """

    name = "insert_closed_stirrup"

    def _apply(self, model, target_selector, *, width_mm: float = 200.0,
               height_mm: float = 300.0, corner_radius_mm: float = 20.0,
               nominal_diameter_mm: Optional[float] = 8.0,
               bar_name: str = "FIV inserted stirrup",
               storey_name: Optional[str] = None,
               host_name_contains: Optional[str] = None, **_ignored) -> OperatorResult:
        context = _model_context(model)
        if context is None:
            raise UnsupportedGeometryError("model has no IfcGeometricRepresentationContext")

        unit_scale = measure.unit_scale_mm(model)
        to_units = 1.0 / unit_scale
        bar, _truth = synthetic.make_closed_stirrup_bar(
            model, context,
            width=float(width_mm) * to_units,
            height=float(height_mm) * to_units,
            corner_radius=float(corner_radius_mm) * to_units,
            name=bar_name,
            nominal_diameter=(
                float(nominal_diameter_mm) * to_units
                if nominal_diameter_mm is not None else None
            ),
        )
        _contain_in_storey(model, bar, storey_name)
        _aggregate_into_host(model, bar, host_name_contains)

        claim = (
            f"inserted closed stirrup {bar.GlobalId}: {width_mm:.0f} x {height_mm:.0f} mm, "
            f"corner radius {corner_radius_mm:.1f} mm, dia {_dia_text(nominal_diameter_mm)}"
        )
        return OperatorResult(
            model=model, operator=self.name,
            params={
                "width_mm": float(width_mm), "height_mm": float(height_mm),
                "corner_radius_mm": float(corner_radius_mm),
                "nominal_diameter_mm": (
                    float(nominal_diameter_mm) if nominal_diameter_mm is not None else None
                ),
            },
            affected_guids=[bar.GlobalId],
            claim=claim,
            expected=[
                {"check": "guid_present", "guid": bar.GlobalId, "ifc_class": "IfcReinforcingBar"},
                # Closed loop: neither end may read as a terminal hook.
                {"check": "hook_absent", "guid": bar.GlobalId},
                _diameter_expectation(bar.GlobalId, nominal_diameter_mm),
            ],
        )
