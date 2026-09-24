"""
A6  - room or corridor ceiling dropped below the BNBC minimum.

Mechanism, in the order the checker reads the height:

  1. the space's own Height / NetHeight / GrossHeight quantity, rewritten;
  2. failing that, the Depth of the space's vertical extrusion, resized
     privately.

Taking them in that order matters. A space that carries a height quantity is
measured from the quantity, so lowering only its geometry would leave the
stated height untouched and the defect invisible; a space with no quantity
is measured from geometry, so there the extrusion is the only thing worth
editing.

A quantity set shared by more than one space is never edited  - that would
silently shorten every room sharing it, and the mutation record would
describe one element while the file changed many.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import set_extrusion_depth_private
from .helpers import length_unit_scale, resolve_body_items, sorted_by_guid, to_mm

RULE_ID = "A6"
CLAUSE = ("BNBC 2020 Part 3 Sec 1.14.2.1(a)  - a habitable room shall have a ceiling height "
          "of at least 2.75 m; Part 4 Sec 3.7.3  - an egress corridor at least 2.4 m")
DOMAIN = "architectural"
ELEMENT = "IfcSpace"

MIN_HABITABLE_MM = 2750.0
MIN_CORRIDOR_MM = 2400.0
# Comfortably under the limit, so the violation survives rounding.
REDUCTION_BELOW_LIMIT_MM = 250.0
MIN_PLAUSIBLE_HEIGHT_MM = 1200.0

_HEIGHT_QUANTITY_NAMES = ("Height", "NetHeight", "GrossHeight")
_CORRIDOR_KEYWORDS = ("corridor", "hallway", "passage", "lobby", "vestibule", "circulat",
                      "gang", "overloop", "hal ", "vest", "entry", "entrance", "entree")
_HABITABLE_KEYWORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ",
    "ward", "waiting", "activity", "lounge", "conference", "meeting", "exam",
    "consult", "operat", "library", "dormitor", "reception", "lab",
    "therapy", "team rm", "break rm", "cubicle", "work station", "workstation",
    "treatment", "clinic", "nurse", "kantoor", "slaapkamer", "woonkamer",
    "eetkamer", "werkkamer", "verblijf",
)


def _space_text(space) -> str:
    return " ".join(
        str(getattr(space, a, None) or "") for a in ("LongName", "Name")
    ).lower()


def _kind(space) -> str | None:
    """'corridor' | 'habitable' | None  - mirrors the checker's own reading."""
    text = _space_text(space)
    if not text.strip():
        return None
    if any(k in text for k in _CORRIDOR_KEYWORDS):
        return "corridor"
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return "habitable"
    return None


def _height_quantity(model: ifcopenshell.file, space):
    """(quantity item, quantity-set name) for an exclusively-owned height."""
    for rel in getattr(space, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        qset = rel.RelatingPropertyDefinition
        if qset is None or not qset.is_a("IfcElementQuantity"):
            continue
        # Shared between spaces: editing it would change rooms this
        # mutation does not name.
        if len(rel.RelatedObjects) != 1:
            continue
        for item in qset.Quantities or ():
            if item.is_a("IfcQuantityLength") and item.Name in _HEIGHT_QUANTITY_NAMES:
                if item.LengthValue:
                    return item, qset.Name
    return None, None


def _vertical_extrusion(space):
    for item in resolve_body_items(space):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        direction = getattr(item.ExtrudedDirection, "DirectionRatios", None)
        if direction is not None and abs(direction[2]) < 0.9:
            continue
        return item
    return None


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    out = []
    for space in sorted_by_guid(model.by_type("IfcSpace")):
        if space.GlobalId in exclude:
            continue
        kind = _kind(space)
        if kind is None:
            continue
        quantity, qset_name = _height_quantity(model, space)
        if quantity is not None:
            height_mm = to_mm(model, float(quantity.LengthValue), scale)
            mode = "quantity"
        else:
            solid = _vertical_extrusion(space)
            if solid is None:
                continue
            height_mm = to_mm(model, float(solid.Depth), scale)
            mode = "geometry"
            qset_name = None
        if height_mm < MIN_PLAUSIBLE_HEIGHT_MM:
            continue
        out.append((space, kind, mode, qset_name, height_mm))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        total = len(model.by_type("IfcSpace"))
        return Applicability(
            False,
            f"no IfcSpace that is both identifiable as a habitable room or corridor and "
            f"carries an editable height ({total} IfcSpace total)",
        )
    return Applicability(True, f"{len(usable)} room(s) or corridor(s) with an editable height")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    def limit_for(kind):
        return MIN_HABITABLE_MM if kind == "habitable" else MIN_CORRIDOR_MM

    compliant = [u for u in usable if u[4] >= limit_for(u[1])]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for space, kind, mode, qset_name, height_mm in pool:
        limit = limit_for(kind)
        already_bad = space.GlobalId not in compliant_ids
        out.append(ScoredTarget(
            global_id=space.GlobalId,
            # Habitable rooms carry the higher limit and are where people
            # live; a quantity edit outranks a geometry edit as the cleaner
            # change. Tallest first within that.
            score=(1e6 if kind == "habitable" else 0.0)
                  + (1e5 if mode == "quantity" else 0.0) + height_mm,
            justification=(
                f"{kind} '{space.LongName or space.Name}' at {height_mm:.0f}mm "
                f"(limit {limit:.0f}mm), height held in the {mode}"
                + (" (ALREADY under the limit before injection)" if already_bad else "")
            ),
            element_ids=(space.id(),),
            extra={
                "kind": kind,
                "mode": mode,
                "quantity_set": qset_name,
                "before_height_mm": height_mm,
                "limit_mm": limit,
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    space = model.by_guid(target.global_id)
    limit_mm = target.extra["limit_mm"]
    before_mm = target.extra["before_height_mm"]

    new_height_mm = float(params.get("new_height_mm", limit_mm - REDUCTION_BELOW_LIMIT_MM))
    # Never raise a ceiling: that would be a correction, not a defect.
    new_height_mm = min(new_height_mm, before_mm)

    if target.extra["mode"] == "quantity":
        quantity, _ = _height_quantity(model, space)
        quantity.LengthValue = float(new_height_mm) * scale
        attribute = f"{target.extra['quantity_set']}.{quantity.Name}"
        mechanism = "rewrote the space's own height quantity"
    else:
        set_extrusion_depth_private(model, space, new_height_mm, scale)
        attribute = "IfcExtrudedAreaSolid.Depth (space height)"
        mechanism = "private extrusion-depth resize (swept solid)"

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this space was already under the {limit_mm:.0f}mm limit before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcSpace",
        target_global_id=target.global_id,
        attribute=attribute,
        before=before_mm,
        after=new_height_mm,
        clause=CLAUSE,
        description=(
            f"Ceiling height of {target.extra['kind']} space "
            f"'{space.LongName or space.Name}' lowered from {before_mm:.0f}mm to "
            f"{new_height_mm:.0f}mm, below the {limit_mm:.0f}mm minimum{note}"
        ),
        extra={
            "element_id": space.id(),
            "kind": target.extra["kind"],
            "limit_mm": limit_mm,
            "mode": target.extra["mode"],
            "quantity_set": target.extra["quantity_set"],
            "already_noncompliant_before": already_bad,
            "mechanism": mechanism,
        },
    )
