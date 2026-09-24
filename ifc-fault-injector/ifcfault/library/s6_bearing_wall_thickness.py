"""
S6  - load-bearing wall specified thinner than BNBC allows.

Mechanism: the wall's material layer set is copied privately and its layers
are scaled down so the nominal thickness falls under the limit. The nominal
thickness IS the layer-set total  - that is the number Sec 7.4.9.1 speaks of
and the number a checker reads first  - so this one edit carries the defect.

The copy is the point. A layer set is shared by every wall of the same type
(one model in this corpus has 132 walls on a single sand-lime layer set), so
scaling it in place would thin every one of them while the record named one.
A new layer set, a new usage and a new association relationship are created
for the target wall alone, and the wall is removed from the shared
association.

Which limit applies is decided from the layer materials, exactly as the
checker decides it: masonry walls take the 250 mm nominal minimum of
Sec 7.4.9.1, concrete walls the 1/25-of-height rule of Sec 6.6.5.3.1.

The wall's body geometry is deliberately NOT resized. A wall whose declared
build-up no longer matches its drawn thickness is precisely the real-world
defect this rule is about  - a wall type edited down without the model being
rebuilt - and the mutation record says so rather than hiding it.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .helpers import (
    length_unit_scale, prop_value, psets_of, resolve_body_items, sorted_by_guid,
    storey_of, to_mm,
)

RULE_ID = "S6"
CLAUSE = ("BNBC 2020 Part 6 Sec 7.4.9.1  - the nominal thickness of a masonry bearing wall "
          "shall not be less than 250 mm; Sec 6.6.5.3.1  - a concrete bearing wall shall not "
          "be thinner than 1/25 of its supported height, nor less than 100 mm")
DOMAIN = "structural"
ELEMENT = "IfcWall"

MIN_MASONRY_MM = 250.0
MIN_CONCRETE_MM = 100.0
CONCRETE_HEIGHT_RATIO = 25.0
# Comfortably under the limit, so the violation survives rounding.
REDUCTION_OF_LIMIT = 0.75
MIN_PLAUSIBLE_THICKNESS_MM = 40.0
MAX_PLAUSIBLE_THICKNESS_MM = 2000.0

_MASONRY_KEYWORDS = ("masonry", "brick", "block", "cmu", "clay", "metselwerk",
                     "baksteen", "kalkzandsteen", "mw-", "mauerwerk")
_CONCRETE_KEYWORDS = ("concrete", "beton", "reinforced")


def _layer_set_of(wall):
    """(association rel, usage or None, layer set) for the wall, or a triple of None."""
    for rel in getattr(wall, "HasAssociations", None) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = rel.RelatingMaterial
        if material is None:
            continue
        if material.is_a("IfcMaterialLayerSetUsage"):
            return rel, material, material.ForLayerSet
        if material.is_a("IfcMaterialLayerSet"):
            return rel, None, material
    return None, None, None


def _classify(layer_set) -> str | None:
    names = " ".join(
        (layer.Material.Name if layer.Material and layer.Material.Name else "")
        for layer in layer_set.MaterialLayers
    ).lower()
    if not names.strip():
        return None
    if any(k in names for k in _MASONRY_KEYWORDS):
        return "masonry"
    if any(k in names for k in _CONCRETE_KEYWORDS):
        return "concrete"
    return None


def _is_load_bearing(wall) -> bool:
    for pdef in psets_of(wall):
        if pdef.Name == "Pset_WallCommon":
            return prop_value(pdef, "LoadBearing") is True
    return False


def _wall_height_mm(model, wall, scale) -> float | None:
    """Supported height of the wall, in mm.

    Read the way the checker reads it: the wall's own vertical extrusion
    first, storey spacing only as a fallback. Deriving the limit from a
    different height than the checker uses would produce an "injected"
    thickness that is still compliant.
    """
    for item in resolve_body_items(wall):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        direction = getattr(item.ExtrudedDirection, "DirectionRatios", None)
        if direction is not None and abs(direction[2]) < 0.9:
            continue
        if item.Depth:
            return to_mm(model, float(item.Depth), scale)

    own = storey_of(model, wall)
    if own is None or own.Elevation is None:
        return None
    above = [
        s.Elevation for s in model.by_type("IfcBuildingStorey")
        if s.Elevation is not None and s.Elevation > own.Elevation
    ]
    if not above:
        return None
    return to_mm(model, min(above) - own.Elevation, scale)


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    out = []
    for wall in sorted_by_guid(model.by_type("IfcWall")):
        if wall.GlobalId in exclude or not _is_load_bearing(wall):
            continue
        rel, usage, layer_set = _layer_set_of(wall)
        if layer_set is None or not layer_set.MaterialLayers:
            continue
        kind = _classify(layer_set)
        if kind is None:
            continue
        total_native = sum(float(layer.LayerThickness or 0.0) for layer in layer_set.MaterialLayers)
        if total_native <= 0:
            continue
        thickness_mm = to_mm(model, total_native, scale)
        if not (MIN_PLAUSIBLE_THICKNESS_MM <= thickness_mm <= MAX_PLAUSIBLE_THICKNESS_MM):
            continue
        if kind == "masonry":
            limit_mm = MIN_MASONRY_MM
        else:
            height_mm = _wall_height_mm(model, wall, scale)
            limit_mm = (max(MIN_CONCRETE_MM, height_mm / CONCRETE_HEIGHT_RATIO)
                        if height_mm else MIN_CONCRETE_MM)
        out.append((wall, rel, usage, layer_set, kind, thickness_mm, limit_mm))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        walls = model.by_type("IfcWall")
        bearing = sum(1 for w in walls if _is_load_bearing(w))
        return Applicability(
            False,
            f"no load-bearing IfcWall with an identifiable material layer set to thin "
            f"({len(walls)} IfcWall, {bearing} flagged load-bearing)",
        )
    return Applicability(True, f"{len(usable)} load-bearing wall(s) with a thinnable layer set")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    compliant = [u for u in usable if u[5] >= u[6]]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for wall, _rel, _usage, layer_set, kind, thickness_mm, limit_mm in pool:
        already_bad = wall.GlobalId not in compliant_ids
        shared = len(model.get_inverse(layer_set))
        out.append(ScoredTarget(
            global_id=wall.GlobalId,
            # Masonry carries the flat 250 mm limit and is where this clause
            # bites hardest; the thickest wall has the most to lose.
            score=(1e6 if kind == "masonry" else 0.0) + thickness_mm,
            justification=(
                f"{kind} bearing wall {thickness_mm:.0f}mm thick over "
                f"{len(layer_set.MaterialLayers)} layer(s) (limit {limit_mm:.0f}mm); "
                f"its layer set is referenced {shared} time(s), so a private copy is made"
                + (" (ALREADY under the limit before injection)" if already_bad else "")
            ),
            element_ids=(wall.id(),),
            extra={
                "kind": kind,
                "before_thickness_mm": thickness_mm,
                "limit_mm": limit_mm,
                "layer_count": len(layer_set.MaterialLayers),
                "layer_set_references": shared,
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    wall = model.by_guid(target.global_id)
    rel, usage, layer_set = _layer_set_of(wall)
    limit_mm = target.extra["limit_mm"]
    before_mm = target.extra["before_thickness_mm"]

    new_thickness_mm = float(params.get("new_thickness_mm", limit_mm * REDUCTION_OF_LIMIT))
    new_thickness_mm = max(MIN_PLAUSIBLE_THICKNESS_MM, min(new_thickness_mm, before_mm))
    factor = new_thickness_mm / before_mm if before_mm else 1.0

    # Every layer shrinks proportionally, so the build-up stays a sensible
    # wall rather than one layer collapsing to nothing.
    new_layers = []
    for layer in layer_set.MaterialLayers:
        new_layers.append(model.create_entity(
            "IfcMaterialLayer",
            Material=layer.Material,
            LayerThickness=float(layer.LayerThickness or 0.0) * factor,
            IsVentilated=layer.IsVentilated,
        ))
    new_layer_set = model.create_entity(
        "IfcMaterialLayerSet", MaterialLayers=tuple(new_layers),
        LayerSetName=layer_set.LayerSetName,
    )

    if usage is not None:
        new_material = model.create_entity(
            "IfcMaterialLayerSetUsage", ForLayerSet=new_layer_set,
            LayerSetDirection=usage.LayerSetDirection, DirectionSense=usage.DirectionSense,
            OffsetFromReferenceLine=usage.OffsetFromReferenceLine,
        )
    else:
        new_material = new_layer_set

    # Detach this wall from the shared association and give it its own.
    remaining = [o for o in rel.RelatedObjects if o.id() != wall.id()]
    if remaining:
        rel.RelatedObjects = tuple(remaining)
        model.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=rel.OwnerHistory,
            Name=rel.Name, Description=rel.Description,
            RelatedObjects=(wall,), RelatingMaterial=new_material,
        )
        detach = f"detached from an association shared with {len(remaining)} other element(s)"
    else:
        rel.RelatingMaterial = new_material
        detach = "the association named only this wall, so it was repointed in place"

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this wall was already under the {limit_mm:.0f}mm limit before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcWall",
        target_global_id=target.global_id,
        attribute="IfcMaterialLayerSet nominal thickness",
        before=before_mm,
        after=new_thickness_mm,
        clause=CLAUSE,
        description=(
            f"Nominal thickness of this {target.extra['kind']} bearing wall reduced from "
            f"{before_mm:.0f}mm to {new_thickness_mm:.0f}mm across "
            f"{len(new_layers)} layer(s), below the {limit_mm:.0f}mm minimum; the body "
            f"geometry is left at its drawn size, so the declared build-up and the drawn "
            f"wall now disagree{note}"
        ),
        extra={
            "element_id": wall.id(),
            "kind": target.extra["kind"],
            "limit_mm": limit_mm,
            "layer_count": len(new_layers),
            "layer_scale_factor": factor,
            "association": detach,
            "already_noncompliant_before": already_bad,
            "mechanism": "private IfcMaterialLayerSet copy with proportionally scaled layers",
        },
    )
