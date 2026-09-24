"""
S10  - footing cast thinner than BNBC allows.

Mechanism: set a new extrusion Depth on the footing's own body, keeping the
plan profile, position and direction. A footing is extruded upward from its
plan outline, so the Depth IS the thickness and one number carries the whole
defect  - no geometry rebuild, no relationships touched.

The new thickness is derived from the limit that applies to this footing
(150 mm on soil, 300 mm on piles) rather than fixed, so the result is under
the limit whichever kind of footing was chosen.

Piles classified as IfcFooting  - several real exports do this  - are
excluded: a pile is not a footing, and its length is not a thickness.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import set_extrusion_depth_private
from .helpers import length_unit_scale, resolve_body_items, sorted_by_guid, to_mm

RULE_ID = "S10"
CLAUSE = ("BNBC 2020 Part 6 Sec 6.8.7  - depth of footing above bottom reinforcement shall "
          "not be less than 150 mm for footings on soil, nor 300 mm for footings on piles")
DOMAIN = "structural"
ELEMENT = "IfcFooting"

MIN_ON_SOIL_MM = 150.0
MIN_ON_PILES_MM = 300.0
# Fraction of the governing limit the footing is reduced to, so the result is
# clearly under it rather than borderline.
REDUCTION_OF_LIMIT = 0.6
MIN_PLAUSIBLE_THICKNESS_MM = 50.0

_PILE_CAP_KEYWORDS = ("pile cap", "pilecap", "poer", "paalkop")
_PILE_KEYWORDS = ("pile", "paal")


def _name_text(element) -> str:
    return " ".join(
        str(getattr(element, a, None) or "") for a in ("Name", "ObjectType")
    ).lower()


def _support(model: ifcopenshell.file, footing) -> str | None:
    """'pile' | 'soil', or None when the element is itself a pile."""
    name = _name_text(footing)
    if any(k in name for k in _PILE_CAP_KEYWORDS):
        return "pile"
    if any(k in name for k in _PILE_KEYWORDS):
        return None
    return "pile" if model.by_type("IfcPile") else "soil"


def _vertical_extrusion(footing):
    for item in resolve_body_items(footing):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        direction = getattr(item.ExtrudedDirection, "DirectionRatios", None)
        if direction is not None and abs(direction[2]) < 0.9:
            continue
        return item
    return None


def _plan_min(profile) -> float | None:
    if profile is None:
        return None
    if profile.is_a("IfcRectangleProfileDef"):
        return min(float(profile.XDim), float(profile.YDim))
    if profile.is_a("IfcCircleProfileDef") or profile.is_a("IfcCircleHollowProfileDef"):
        return float(profile.Radius) * 2.0
    curve = getattr(profile, "OuterCurve", None)
    points = []
    if curve is not None and curve.is_a("IfcPolyline"):
        points = [p.Coordinates[:2] for p in curve.Points]
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(max(xs) - min(xs), max(ys) - min(ys))


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    out = []
    for footing in sorted_by_guid(model.by_type("IfcFooting")):
        if footing.GlobalId in exclude:
            continue
        support = _support(model, footing)
        if support is None:
            continue
        solid = _vertical_extrusion(footing)
        if solid is None:
            continue
        thickness_mm = to_mm(model, float(solid.Depth), scale)
        plan_native = _plan_min(solid.SweptArea)
        plan_mm = to_mm(model, plan_native, scale) if plan_native else None
        # Taller than it is wide: a pile, a pier or a pedestal, not a footing.
        if plan_mm and thickness_mm > plan_mm:
            continue
        if thickness_mm < MIN_PLAUSIBLE_THICKNESS_MM:
            continue
        out.append((footing, support, thickness_mm, plan_mm))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        total = len(model.by_type("IfcFooting"))
        return Applicability(
            False,
            f"no IfcFooting with a vertically extruded body whose depth is a thickness "
            f"({total} IfcFooting total)",
        )
    return Applicability(True, f"{len(usable)} footing(s) with a resizable thickness")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    def limit_for(support):
        return MIN_ON_PILES_MM if support == "pile" else MIN_ON_SOIL_MM

    compliant = [u for u in usable if u[2] >= limit_for(u[1])]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for footing, support, thickness_mm, plan_mm in pool:
        limit = limit_for(support)
        already_bad = footing.GlobalId not in compliant_ids
        out.append(ScoredTarget(
            global_id=footing.GlobalId,
            # Largest footing first: the biggest pad carries the most load,
            # so under-sizing it is the most consequential defect.
            score=plan_mm or 0.0,
            justification=(
                f"{support}-supported footing {thickness_mm:.0f}mm thick"
                + (f", {plan_mm:.0f}mm across" if plan_mm else "")
                + f"; limit is {limit:.0f}mm"
                + (" (ALREADY under the limit before injection)" if already_bad else "")
            ),
            element_ids=(footing.id(),),
            extra={
                "support": support,
                "before_thickness_mm": thickness_mm,
                "plan_min_mm": plan_mm,
                "limit_mm": limit,
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    footing = model.by_guid(target.global_id)
    limit_mm = target.extra["limit_mm"]
    before_mm = target.extra["before_thickness_mm"]

    new_thickness_mm = float(
        params.get("new_thickness_mm", max(MIN_PLAUSIBLE_THICKNESS_MM, limit_mm * REDUCTION_OF_LIMIT))
    )
    # Never make the footing thicker than it already was: that would be a
    # correction, not a defect.
    new_thickness_mm = min(new_thickness_mm, before_mm)

    recorded_before_mm = set_extrusion_depth_private(model, footing, new_thickness_mm, scale)
    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this footing was already under the {limit_mm:.0f}mm limit before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcFooting",
        target_global_id=target.global_id,
        attribute="IfcExtrudedAreaSolid.Depth (footing thickness)",
        before=recorded_before_mm,
        after=new_thickness_mm,
        clause=CLAUSE,
        description=(
            f"Footing thickness reduced from {recorded_before_mm:.0f}mm to "
            f"{new_thickness_mm:.0f}mm, below the {limit_mm:.0f}mm minimum for a "
            f"{target.extra['support']}-supported footing{note}"
        ),
        extra={
            "element_id": footing.id(),
            "support": target.extra["support"],
            "limit_mm": limit_mm,
            "plan_min_mm": target.extra["plan_min_mm"],
            "already_noncompliant_before": already_bad,
            "mechanism": "private extrusion-depth resize (swept solid)",
        },
    )
