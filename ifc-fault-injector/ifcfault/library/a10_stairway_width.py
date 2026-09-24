"""
A10  - stair flight narrowed below the minimum egress width.

Mechanism: a stair flight is not one solid. Authoring tools export it as a
run of small extrusions, one per tread, each a rectangle of
(flight width x going) swept vertically. Narrowing the flight therefore
means narrowing every tread, so this rule rebuilds the flight's Body with a
PRIVATE copy of each solid whose profile has been scaled in the width
direction only. The going, the rise, the positions and the number of treads
are untouched, so the result is the same stair, narrower.

Which of a profile's two dimensions is the width is decided by measurement,
not assumption: the flight's width is the dimension that recurs across the
treads (every tread is as wide as the flight, while goings vary at a
winder), so the modal larger dimension is taken as the width and only
dimensions matching it are scaled.

Profiles are always copied before being scaled. Tread profiles are shared
between flights in every model of this corpus, and editing one in place
would narrow every stair in the building while the record named one.
"""
from __future__ import annotations

from collections import Counter

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .helpers import length_unit_scale, mm, sorted_by_guid, to_mm

RULE_ID = "A10"
CLAUSE = ("BNBC 2020 Part 3 Sec 1.14.5.1 with Part 4 Table 4.3.6  - the clear width of an "
          "egress stairway shall be at least 1120 mm")
DOMAIN = "architectural"
ELEMENT = "IfcStairFlight"

MIN_WIDTH_MM = 1120.0
# Comfortably under the limit, so the violation survives rounding.
REDUCTION_BELOW_LIMIT_MM = 220.0
MIN_PLAUSIBLE_WIDTH_MM = 500.0
MAX_PLAUSIBLE_WIDTH_MM = 6000.0
# How close a profile dimension must be to the flight width to count as the
# width rather than the going.
WIDTH_MATCH_TOLERANCE = 0.02


def _body_representation(element):
    rep = getattr(element, "Representation", None)
    if rep is None:
        return None
    for r in rep.Representations:
        if r.RepresentationIdentifier == "Body":
            return r
    return None


def _rectangle_solids(representation):
    """(solid, profile) for every rectangle-profiled extrusion in the Body.

    IfcMappedItem is followed one level, since a flight is often exported as
    a mapped run of treads.
    """
    out = []
    if representation is None:
        return out
    for item in representation.Items:
        items = (item.MappingSource.MappedRepresentation.Items
                 if item.is_a("IfcMappedItem") else (item,))
        for sub in items:
            if sub.is_a("IfcExtrudedAreaSolid") and sub.SweptArea.is_a("IfcRectangleProfileDef"):
                out.append((sub, sub.SweptArea))
    return out


def _flight_width(pairs) -> float | None:
    """The dimension the treads share, in native units."""
    if not pairs:
        return None
    larger = Counter(
        round(max(float(profile.XDim), float(profile.YDim)), 6) for _, profile in pairs
    )
    # Ties break on the larger dimension so the answer never depends on
    # dict ordering.
    return max(larger.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    out = []
    for flight in sorted_by_guid(model.by_type("IfcStairFlight")):
        if flight.GlobalId in exclude:
            continue
        representation = _body_representation(flight)
        pairs = _rectangle_solids(representation)
        width_native = _flight_width(pairs)
        if width_native is None:
            continue
        width_mm = to_mm(model, width_native, scale)
        if not (MIN_PLAUSIBLE_WIDTH_MM <= width_mm <= MAX_PLAUSIBLE_WIDTH_MM):
            continue
        out.append((flight, representation, pairs, width_mm))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        flights = len(model.by_type("IfcStairFlight"))
        stairs = len(model.by_type("IfcStair"))
        return Applicability(
            False,
            f"no IfcStairFlight whose treads are rectangle-profiled extrusions to narrow "
            f"({flights} IfcStairFlight, {stairs} IfcStair total)",
        )
    return Applicability(True, f"{len(usable)} stair flight(s) with a narrowable tread run")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    compliant = [u for u in usable if u[3] >= MIN_WIDTH_MM]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for flight, _representation, pairs, width_mm in pool:
        already_bad = flight.GlobalId not in compliant_ids
        out.append(ScoredTarget(
            global_id=flight.GlobalId,
            # The widest flight has the most room to lose, and the one built
            # from the most treads is the most thoroughly rebuilt.
            score=width_mm + len(pairs),
            justification=(
                f"stair flight {width_mm:.0f}mm wide across {len(pairs)} tread solid(s) "
                f"(limit {MIN_WIDTH_MM:.0f}mm)"
                + (" (ALREADY under the limit before injection)" if already_bad else "")
            ),
            element_ids=(flight.id(),),
            extra={
                "before_width_mm": width_mm,
                "tread_solid_count": len(pairs),
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    flight = model.by_guid(target.global_id)
    before_width_mm = target.extra["before_width_mm"]

    new_width_mm = float(params.get("new_width_mm", MIN_WIDTH_MM - REDUCTION_BELOW_LIMIT_MM))
    new_width_mm = max(MIN_PLAUSIBLE_WIDTH_MM, min(new_width_mm, before_width_mm))

    representation = _body_representation(flight)
    pairs = _rectangle_solids(representation)
    width_native = _flight_width(pairs)
    new_width_native = mm(model, new_width_mm, scale)
    tolerance = abs(width_native) * WIDTH_MATCH_TOLERANCE

    new_items = []
    rebuilt = 0
    for item in representation.Items:
        items = (list(item.MappingSource.MappedRepresentation.Items)
                 if item.is_a("IfcMappedItem") else [item])
        for sub in items:
            if not (sub.is_a("IfcExtrudedAreaSolid")
                    and sub.SweptArea.is_a("IfcRectangleProfileDef")):
                new_items.append(sub)
                continue
            profile = sub.SweptArea
            x_dim, y_dim = float(profile.XDim), float(profile.YDim)
            # Only the dimension that IS the flight width is scaled; the
            # going is left exactly as it was.
            new_x = new_width_native if abs(x_dim - width_native) <= tolerance else x_dim
            new_y = new_width_native if abs(y_dim - width_native) <= tolerance else y_dim
            if new_x == x_dim and new_y == y_dim:
                new_items.append(sub)
                continue
            new_profile = model.create_entity(
                "IfcRectangleProfileDef", ProfileType=profile.ProfileType,
                ProfileName=profile.ProfileName, Position=profile.Position,
                XDim=new_x, YDim=new_y,
            )
            new_items.append(model.create_entity(
                "IfcExtrudedAreaSolid", SweptArea=new_profile, Position=sub.Position,
                ExtrudedDirection=sub.ExtrudedDirection, Depth=sub.Depth,
            ))
            rebuilt += 1

    new_body = model.create_entity(
        "IfcShapeRepresentation", ContextOfItems=representation.ContextOfItems,
        RepresentationIdentifier="Body", RepresentationType="SweptSolid",
        Items=tuple(new_items),
    )
    flight.Representation = model.create_entity(
        "IfcProductDefinitionShape", Name=None, Description=None,
        Representations=tuple(
            new_body if r.id() == representation.id() else r
            for r in flight.Representation.Representations
        ),
    )

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this flight was already under the {MIN_WIDTH_MM:.0f}mm limit before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcStairFlight",
        target_global_id=target.global_id,
        attribute="stair flight width",
        before=before_width_mm,
        after=new_width_mm,
        clause=CLAUSE,
        description=(
            f"Stair flight narrowed from {before_width_mm:.0f}mm to {new_width_mm:.0f}mm across "
            f"{rebuilt} tread solid(s), below the {MIN_WIDTH_MM:.0f}mm egress minimum{note}"
        ),
        extra={
            "element_id": flight.id(),
            "min_width_mm": MIN_WIDTH_MM,
            "tread_solids_rebuilt": rebuilt,
            "tread_solid_count": target.extra["tread_solid_count"],
            "already_noncompliant_before": already_bad,
            "mechanism": "private per-tread IfcRectangleProfileDef copy, scaled in the width axis",
        },
    )
