"""
S7  - special moment frame column section reduced below the BNBC geometric
limits.

Mechanism: private rectangular profile swap, as S2 does, but sized to break
BOTH limits of BNBC Sec 8.3.5.1 at once  - the shortest side goes under
300 mm AND the short/long ratio goes under 0.4. A single square section
cannot do that (a square has ratio 1.0), so the replacement is deliberately
oblong: the blade column that looks adequate in plan and is not.

Only concrete columns are touched. Sec 8.3.5.1 is a reinforced-concrete
detailing clause, so narrowing a steel section would be a defect the clause
has nothing to say about.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import replace_profile_private
from .helpers import columns_with_size, length_unit_scale, resolve_body_items, sorted_by_guid

RULE_ID = "S7"
CLAUSE = ("BNBC 2020 Part 6 Sec 8.3.5.1  - a special moment frame column shall have a "
          "shortest cross-sectional dimension of at least 300 mm and a shortest-to-"
          "perpendicular dimension ratio of at least 0.4")
DOMAIN = "structural"
ELEMENT = "IfcColumn"

MIN_DIMENSION_MM = 300.0
MIN_RATIO = 0.4

# Comfortably under 300 mm, so the violation survives rounding.
NEW_SHORT_MM = 250.0
# 250 / 700 = 0.36, under the 0.4 ratio as well.
NEW_LONG_MM = 700.0
MIN_PLAUSIBLE_HEIGHT_MM = 2000.0

_CONCRETE_KEYWORDS = ("concrete", "beton", "reinforced")
_STEEL_KEYWORDS = ("steel", "staal", "metal", "wide flange", "universal column", "uc-",
                   "hss", "shs", "chs", "s235", "s275", "s355")


def _material_text(element) -> str:
    names = []
    for rel in getattr(element, "HasAssociations", None) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = rel.RelatingMaterial
        if material is None:
            continue
        if material.is_a("IfcMaterial"):
            names.append(material.Name or "")
        elif material.is_a("IfcMaterialList"):
            names.extend(m.Name or "" for m in material.Materials)
        elif material.is_a("IfcMaterialLayerSetUsage"):
            names.extend((layer.Material.Name if layer.Material else "")
                         for layer in material.ForLayerSet.MaterialLayers)
        elif material.is_a("IfcMaterialLayerSet"):
            names.extend((layer.Material.Name if layer.Material else "")
                         for layer in material.MaterialLayers)
    text = " ".join(n for n in names if n)
    if not text.strip():
        # No material association: the section name is the only evidence,
        # and this corpus names concrete sections explicitly.
        text = " ".join(str(getattr(element, a, None) or "") for a in ("Name", "ObjectType"))
    return text.lower()


def _is_concrete(element) -> bool:
    text = _material_text(element)
    if any(k in text for k in _STEEL_KEYWORDS):
        return False
    return any(k in text for k in _CONCRETE_KEYWORDS)


def _has_swept_profile(element) -> bool:
    return any(i.is_a("IfcExtrudedAreaSolid") for i in resolve_body_items(element))


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    out = []
    for size in columns_with_size(model):
        if size.global_id in exclude or not (size.width_mm and size.depth_mm):
            continue
        column = model.by_guid(size.global_id)
        if not _is_concrete(column) or not _has_swept_profile(column):
            continue
        out.append(size)
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        total = len(model.by_type("IfcColumn"))
        concrete = sum(1 for c in sorted_by_guid(model.by_type("IfcColumn")) if _is_concrete(c))
        return Applicability(
            False,
            f"no concrete IfcColumn with a resizable swept profile "
            f"({total} IfcColumn total, {concrete} identifiably concrete)",
        )
    return Applicability(True, f"{len(usable)} concrete column(s) with a resizable swept profile")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    # A column that already breaks the clause is a pre-existing defect, not
    # an injected one, so prefer one that currently complies. Each filter
    # falls back rather than emptying the list.
    compliant = [
        c for c in usable
        if min(c.width_mm, c.depth_mm) >= MIN_DIMENSION_MM
        and min(c.width_mm, c.depth_mm) / max(c.width_mm, c.depth_mm) >= MIN_RATIO
    ]
    pool = compliant if compliant else usable
    compliant_ids = {c.global_id for c in compliant}

    plausible = [c for c in pool if c.length_mm >= MIN_PLAUSIBLE_HEIGHT_MM]
    pool = plausible if plausible else pool

    out = []
    for size in pool:
        short = min(size.width_mm, size.depth_mm)
        long = max(size.width_mm, size.depth_mm)
        already_bad = size.global_id not in compliant_ids
        out.append(ScoredTarget(
            global_id=size.global_id,
            # Tallest first: the column carrying the most storey height is
            # the most consequential frame member to mis-proportion.
            score=size.length_mm,
            justification=(
                f"concrete column {short:.0f}x{long:.0f}mm over a {size.length_mm:.0f}mm height "
                f"-> {NEW_SHORT_MM:.0f}x{NEW_LONG_MM:.0f}mm"
                + (" (ALREADY outside the Sec 8.3.5.1 limits before injection)"
                   if already_bad else "")
            ),
            element_ids=(size.element_id,),
            extra={
                "before_height_mm": size.length_mm,
                "before_short_mm": short,
                "before_long_mm": long,
                "before_ratio": short / long if long else None,
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    column = model.by_guid(target.global_id)

    new_short_mm = float(params.get("new_short_mm", NEW_SHORT_MM))
    new_long_mm = float(params.get("new_long_mm", NEW_LONG_MM))

    replace_profile_private(model, column, new_short_mm, new_long_mm, scale)

    before_short = target.extra["before_short_mm"]
    before_long = target.extra["before_long_mm"]
    new_ratio = new_short_mm / new_long_mm
    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            " (this column was already outside the Sec 8.3.5.1 limits before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcColumn",
        target_global_id=target.global_id,
        attribute="cross-section dimensions",
        before=before_short,
        after=new_short_mm,
        clause=CLAUSE,
        description=(
            f"Special moment frame column section changed from "
            f"{before_short:.0f}x{before_long:.0f}mm to {new_short_mm:.0f}x{new_long_mm:.0f}mm: "
            f"the shortest dimension is now below the {MIN_DIMENSION_MM:.0f}mm minimum and the "
            f"dimension ratio {new_ratio:.2f} is below the {MIN_RATIO:.1f} minimum{note}"
        ),
        extra={
            "element_id": column.id(),
            "height_mm": target.extra["before_height_mm"],
            "before_short_mm": before_short,
            "before_long_mm": before_long,
            "before_ratio": target.extra["before_ratio"],
            "after_short_mm": new_short_mm,
            "after_long_mm": new_long_mm,
            "after_ratio": new_ratio,
            "min_dimension_mm": MIN_DIMENSION_MM,
            "min_ratio": MIN_RATIO,
            "already_noncompliant_before": already_bad,
            "mechanism": "private IfcRectangleProfileDef swap (swept solid)",
        },
    )
