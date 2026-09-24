"""
A7  - room narrowed below the least width BNBC allows.

Mechanism: private rectangular profile swap on the space's own footprint,
the same edit S2 makes to a column section. The room is re-proportioned to a
corridor-thin strip: the new width is under the clause's limit while the new
length is chosen to keep the FLOOR AREA of the original room. That is
deliberate  - the defect under test is the least-dimension limit of
Sec 1.14.2.2, and leaving the area alone keeps the two conditions of that
clause independent, so a checker that passes the area test and fails the
width test is demonstrably reading both.

Only spaces whose footprint is an extruded profile are targeted. A space
exported as nothing but a bounding box has no footprint to re-proportion.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import replace_profile_private
from .helpers import length_unit_scale, resolve_body_items, sorted_by_guid, to_mm

RULE_ID = "A7"
CLAUSE = ("BNBC 2020 Part 3 Sec 1.14.2.2  - a habitable room shall be at least 9.5 m2 with a "
          "least width of 2.9 m; another room in the dwelling at least 5 m2 with a least "
          "width of 2 m")
DOMAIN = "architectural"
ELEMENT = "IfcSpace"

MIN_HABITABLE_WIDTH_MM = 2900.0
MIN_OTHER_WIDTH_MM = 2000.0
# Comfortably under the limit, so the violation survives rounding.
REDUCTION_BELOW_LIMIT_MM = 400.0
MIN_PLAUSIBLE_WIDTH_MM = 500.0
MAX_PLAUSIBLE_WIDTH_MM = 30000.0

_HABITABLE_KEYWORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ",
    "ward", "waiting", "activity", "lounge", "conference", "meeting", "exam",
    "consult", "operat", "library", "dormitor", "reception", "lab",
    "therapy", "team rm", "break rm", "cubicle", "work station", "workstation",
    "treatment", "clinic", "nurse", "kantoor", "slaapkamer", "woonkamer",
    "eetkamer", "werkkamer", "verblijf",
)
_OTHER_ROOM_KEYWORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower",
    "store", "storage", "stor", "kitchen", "pantry", "laundry", "utility",
    "utl", "closet", "janitor", "jan.", "jan ", "badkamer", "keuken",
    "berging", "kast",
)
_OUT_OF_SCOPE_KEYWORDS = (
    "corridor", "hallway", "passage", "lobby", "vestibule", "circulat", "gang",
    "overloop", "hal ", "vest", "entry", "entrance", "entree",
    "stair", "shaft", "riser", "chase", "elevator", "elev", "lift", "duct",
    "roof", "void", "open to below", "garage", "parking", "plant",
    "mechanical", "mech", "electrical", "elec", "server", "tele", "comm.",
    "comm rm", "equip", "instal", "trap", "meterkast", "onben",
)


def _kind(space) -> str | None:
    """'habitable' | 'other' | None  - mirrors the checker's own reading."""
    text = " ".join(
        str(getattr(space, a, None) or "") for a in ("LongName", "Name")
    ).lower()
    if not text.strip():
        return None
    if any(k in text for k in _OUT_OF_SCOPE_KEYWORDS):
        return None
    if any(k in text for k in _OTHER_ROOM_KEYWORDS):
        return "other"
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return "habitable"
    return None


def _profile_points(profile):
    if profile is None:
        return []
    if profile.is_a("IfcRectangleProfileDef"):
        x, y = float(profile.XDim) / 2.0, float(profile.YDim) / 2.0
        return [(-x, -y), (x, -y), (x, y), (-x, y)]
    curve = getattr(profile, "OuterCurve", None)
    if curve is None:
        return []
    if curve.is_a("IfcPolyline"):
        return [tuple(p.Coordinates[:2]) for p in curve.Points]
    if curve.is_a("IfcCompositeCurve"):
        points = []
        for segment in curve.Segments:
            parent = segment.ParentCurve
            if parent.is_a("IfcPolyline"):
                points.extend(tuple(p.Coordinates[:2]) for p in parent.Points)
        return points
    return []


def _polygon_area(points) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _footprint(space):
    """(width native, length native, area native) of the space, or None."""
    for item in resolve_body_items(space):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        points = _profile_points(item.SweptArea)
        if len(points) < 3:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        dx, dy = max(xs) - min(xs), max(ys) - min(ys)
        return min(dx, dy), max(dx, dy), _polygon_area(points)
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
        footprint = _footprint(space)
        if footprint is None:
            continue
        width_mm = to_mm(model, footprint[0], scale)
        length_mm = to_mm(model, footprint[1], scale)
        area_mm2 = to_mm(model, to_mm(model, footprint[2], scale), scale)
        if not (MIN_PLAUSIBLE_WIDTH_MM <= width_mm <= MAX_PLAUSIBLE_WIDTH_MM):
            continue
        out.append((space, kind, width_mm, length_mm, area_mm2))
    return out


def applicable(model: ifcopenshell.file) -> Applicability:
    usable = _usable(model)
    if not usable:
        total = len(model.by_type("IfcSpace"))
        return Applicability(
            False,
            f"no IfcSpace that is both identifiable as a room and has an extruded "
            f"footprint to re-proportion ({total} IfcSpace total)",
        )
    return Applicability(True, f"{len(usable)} room(s) with a re-proportionable footprint")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable = _usable(model, exclude)
    if not usable:
        return []

    def limit_for(kind):
        return MIN_HABITABLE_WIDTH_MM if kind == "habitable" else MIN_OTHER_WIDTH_MM

    compliant = [u for u in usable if u[2] >= limit_for(u[1])]
    pool = compliant if compliant else usable
    compliant_ids = {u[0].GlobalId for u in compliant}

    out = []
    for space, kind, width_mm, length_mm, area_mm2 in pool:
        limit = limit_for(kind)
        already_bad = space.GlobalId not in compliant_ids
        out.append(ScoredTarget(
            global_id=space.GlobalId,
            # Habitable rooms carry the higher limit; the widest room is the
            # one whose narrowing is the most visible change.
            score=(1e6 if kind == "habitable" else 0.0) + width_mm,
            justification=(
                f"{kind} room '{space.LongName or space.Name}' {width_mm:.0f}x{length_mm:.0f}mm "
                f"(limit {limit:.0f}mm least width)"
                + (" (ALREADY under the limit before injection)" if already_bad else "")
            ),
            element_ids=(space.id(),),
            extra={
                "kind": kind,
                "before_width_mm": width_mm,
                "before_length_mm": length_mm,
                "before_area_mm2": area_mm2,
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
    before_width_mm = target.extra["before_width_mm"]
    area_mm2 = target.extra["before_area_mm2"]

    new_width_mm = float(params.get("new_width_mm", limit_mm - REDUCTION_BELOW_LIMIT_MM))
    new_width_mm = max(MIN_PLAUSIBLE_WIDTH_MM, min(new_width_mm, before_width_mm))
    # Keep the room's floor area: only the proportion is the defect.
    new_length_mm = max(new_width_mm, area_mm2 / new_width_mm if new_width_mm else new_width_mm)

    replace_profile_private(model, space, new_width_mm, new_length_mm, scale)

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this room was already under the {limit_mm:.0f}mm limit before injection)")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcSpace",
        target_global_id=target.global_id,
        attribute="footprint least width",
        before=before_width_mm,
        after=new_width_mm,
        clause=CLAUSE,
        description=(
            f"Room '{space.LongName or space.Name}' re-proportioned from "
            f"{before_width_mm:.0f}x{target.extra['before_length_mm']:.0f}mm to "
            f"{new_width_mm:.0f}x{new_length_mm:.0f}mm: the least width is now below the "
            f"{limit_mm:.0f}mm minimum while the floor area "
            f"({area_mm2 / 1e6:.1f} m2) is preserved{note}"
        ),
        extra={
            "element_id": space.id(),
            "kind": target.extra["kind"],
            "limit_mm": limit_mm,
            "before_length_mm": target.extra["before_length_mm"],
            "after_length_mm": new_length_mm,
            "preserved_area_m2": area_mm2 / 1e6,
            "already_noncompliant_before": already_bad,
            "mechanism": "private IfcRectangleProfileDef swap (space footprint)",
        },
    )
