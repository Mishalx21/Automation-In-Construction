"""
A9  - guard or handrail set below the height BNBC requires.

Mechanism: rewrite Pset_RailingCommon.Height. One property, no geometry
rebuild, no relationships touched  - the same clean shape as A1.

A guard is preferred over a handrail: its limit is the higher of the two
(1 m against 0.9 m), and a guard below 1 m is the defect people actually
fall over. The new height is derived from whichever limit applies, so the
result is always under it.

Fallback: where a railing carries no Height property at all, one is created
holding the under-height value, which documents the defect on an element
that previously said nothing about its own height.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import any_owner_history, get_or_create_pset, set_prop_single_value
from .helpers import find_prop, length_unit_scale, mm, psets_of, sorted_by_guid, to_mm

RULE_ID = "A9"
CLAUSE = ("BNBC 2020 Part 3 Sec 1.14.14  - parapets and guardrails enclosing an accessible "
          "flat roof shall be at least 1 m high; Sec 1.14.5.6  - a stair handrail shall be "
          "at least 0.9 m high")
DOMAIN = "architectural"
ELEMENT = "IfcRailing"

MIN_GUARD_MM = 1000.0
MIN_HANDRAIL_MM = 900.0
# Comfortably under the limit, so the violation survives rounding.
REDUCTION_BELOW_LIMIT_MM = 150.0

MIN_PLAUSIBLE_HEIGHT_MM = 300.0
MAX_PLAUSIBLE_HEIGHT_MM = 3000.0

_PSET_NAMES = ("Pset_RailingCommon", "Pset_Railing")
_GUARD_KEYWORDS = ("guard", "balustrade", "parapet", "barrier", "doorvalregel", "traphek",
                   "afscheiding", "hekwerk", "borstwering")
_HANDRAIL_KEYWORDS = ("handrail", "hand rail", "leuning", "trapleuning")


def _kind(railing) -> str | None:
    """'guard' | 'handrail' | None  - mirrors the checker's own reading."""
    predefined = str(getattr(railing, "PredefinedType", None) or "").upper()
    if predefined in ("GUARDRAIL", "BALUSTRADE"):
        return "guard"
    if predefined == "HANDRAIL":
        return "handrail"
    text = " ".join(
        str(getattr(railing, a, None) or "") for a in ("Name", "ObjectType", "Description")
    ).lower()
    if not text.strip():
        return None
    if any(k in text for k in _GUARD_KEYWORDS):
        return "guard"
    if any(k in text for k in _HANDRAIL_KEYWORDS):
        return "handrail"
    return None


def _height_property(railing):
    """(pset, height in native units) for the railing's Height, or (None, None)."""
    for pdef in psets_of(railing):
        if pdef.Name not in _PSET_NAMES:
            continue
        prop = find_prop(pdef, "Height")
        if prop is not None and prop.NominalValue is not None:
            return pdef, prop.NominalValue.wrappedValue
    return None, None


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    out = []
    for railing in sorted_by_guid(model.by_type("IfcRailing")):
        if railing.GlobalId in exclude:
            continue
        kind = _kind(railing)
        if kind is None:
            continue
        pset, native = _height_property(railing)
        height_mm = to_mm(model, float(native), scale) if native is not None else None
        if height_mm is not None and not (
            MIN_PLAUSIBLE_HEIGHT_MM <= height_mm <= MAX_PLAUSIBLE_HEIGHT_MM
        ):
            continue
        out.append((railing, kind, pset, height_mm))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        total = len(model.by_type("IfcRailing"))
        return Applicability(
            False,
            f"no IfcRailing identifiable as a guard or a handrail ({total} IfcRailing total)",
        )
    with_height = sum(1 for u in usable if u[3] is not None)
    return Applicability(
        True,
        f"{len(usable)} railing(s) identifiable as a guard or a handrail "
        f"({with_height} already carrying a Height property)",
    )


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    def limit_for(kind):
        return MIN_GUARD_MM if kind == "guard" else MIN_HANDRAIL_MM

    # Prefer a railing that currently complies  - lowering one that is
    # already short is a pre-existing defect, not an injected one.
    compliant = [u for u in usable if u[3] is not None and u[3] >= limit_for(u[1])]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for railing, kind, pset, height_mm in pool:
        limit = limit_for(kind)
        already_bad = railing.GlobalId not in compliant_ids
        has_property = pset is not None
        out.append(ScoredTarget(
            global_id=railing.GlobalId,
            # A guard outranks a handrail (higher limit, worse consequence),
            # and an existing Height property outranks one that must be
            # created, because overwriting is the cleaner edit.
            score=(1000.0 if kind == "guard" else 0.0) + (100.0 if has_property else 0.0),
            justification=(
                f"{kind}"
                + (f" at {height_mm:.0f}mm" if height_mm is not None else " with no Height property")
                + f"; limit is {limit:.0f}mm"
                + ("" if has_property else " (a Height property will be created)")
                + (" (ALREADY under the limit before injection)" if already_bad and height_mm else "")
            ),
            element_ids=(railing.id(),),
            extra={
                "kind": kind,
                "before_height_mm": height_mm,
                "limit_mm": limit,
                "pset_name": pset.Name if pset is not None else None,
                "already_noncompliant": already_bad and height_mm is not None,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    railing = model.by_guid(target.global_id)
    limit_mm = target.extra["limit_mm"]
    before_mm = target.extra["before_height_mm"]

    new_height_mm = float(params.get("new_height_mm", limit_mm - REDUCTION_BELOW_LIMIT_MM))
    if before_mm is not None:
        # Never raise a railing: that would be a correction, not a defect.
        new_height_mm = min(new_height_mm, before_mm)

    pset_name = target.extra["pset_name"] or "Pset_RailingCommon"
    if target.extra["pset_name"]:
        pset = next(p for p in psets_of(railing) if p.Name == pset_name)
        prop = find_prop(pset, "Height")
        # Preserve the file's own measure type rather than forcing one.
        prop.NominalValue = model.create_entity(
            prop.NominalValue.is_a(), mm(model, new_height_mm, scale)
        )
        mechanism = f"overwrote Height in the existing {pset_name}"
    else:
        owner_history = any_owner_history(model)
        pset = get_or_create_pset(model, railing, pset_name, owner_history, guid_seed=RULE_ID)
        set_prop_single_value(
            model, pset, "Height", "IfcPositiveLengthMeasure",
            mm(model, new_height_mm, scale), owner_history,
        )
        mechanism = f"created {pset_name} with an under-height Height"

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this railing was already under the {limit_mm:.0f}mm limit before injection)")
    before_text = f"{before_mm:.0f}mm" if before_mm is not None else "(no Height property)"

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcRailing",
        target_global_id=target.global_id,
        attribute=f"{pset_name}.Height",
        before=before_mm,
        after=new_height_mm,
        clause=CLAUSE,
        description=(
            f"{target.extra['kind'].capitalize()} height set from {before_text} to "
            f"{new_height_mm:.0f}mm, below the {limit_mm:.0f}mm minimum{note}"
        ),
        extra={
            "element_id": railing.id(),
            "kind": target.extra["kind"],
            "limit_mm": limit_mm,
            "pset_name": pset_name,
            "already_noncompliant_before": already_bad,
            "mechanism": mechanism,
        },
    )
