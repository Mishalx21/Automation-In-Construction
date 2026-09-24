"""
A8  - a room's window shrunk until the room no longer gets the daylight and
ventilation opening BNBC requires.

Mechanism: write IfcWindow.OverallWidth and OverallHeight directly. One
element, two attributes, no geometry rebuild and no relationships touched -
the same clean shape as A1.

What makes this rule different from A1 is that the defect is not local to
the element edited. The clause is about the ROOM: the aggregate opening area
in its exterior wall as a percentage of its floor area. So a candidate is
only offered when shrinking that one window actually drops its room below
the percentage  - a room with four generous windows does not become
non-compliant because one of them got smaller, and offering it would produce
a "violation" no checker could find.

The room is reached the way the checker reaches it: window -> host wall ->
the spaces that exterior wall bounds. Where a model does not carry enough of
that linkage the rule declines outright, because a checker reading the same
model would return unknown rather than a violation.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .helpers import (
    get_host_wall, length_unit_scale, mm, prop_value, psets_of, sorted_by_guid, to_mm,
)

RULE_ID = "A8"
CLAUSE = ("BNBC 2020 Part 3 Sec 1.19.6, Table 3.1.12  - the aggregate area of openings in "
          "the exterior wall, excluding doors, shall be at least 15% of the net floor area "
          "of a habitable room, 18% of a kitchen and 10% of a non-habitable space")
DOMAIN = "architectural"
ELEMENT = "IfcWindow"

HABITABLE_PERCENT = 15.0
KITCHEN_PERCENT = 18.0
NON_HABITABLE_PERCENT = 10.0
# The checker declines to measure ratios at all below this linkage coverage.
MIN_LINK_COVERAGE = 0.5
# The window is shrunk to this fraction of its own smaller dimension, which
# leaves a real (if useless) window rather than a zero-area ghost.
SHRINK_FACTOR = 0.25
MIN_WINDOW_DIMENSION_MM = 200.0
MIN_PLAUSIBLE_AREA_M2 = 1.0
MAX_PLAUSIBLE_AREA_M2 = 2000.0

_KITCHEN_KEYWORDS = ("kitchen", "keuken", "pantry")
_HABITABLE_KEYWORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ",
    "ward", "waiting", "activity", "lounge", "conference", "meeting", "exam",
    "consult", "operat", "library", "dormitor", "reception", "lab",
    "therapy", "team rm", "break rm", "cubicle", "work station", "workstation",
    "treatment", "clinic", "nurse", "kantoor", "slaapkamer", "woonkamer",
    "eetkamer", "werkkamer", "verblijf",
)
_NON_HABITABLE_KEYWORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower",
    "store", "storage", "stor", "stair", "utility", "utl", "closet",
    "janitor", "jan.", "jan ", "laundry", "corridor", "hallway", "passage",
    "lobby", "vestibule", "circulat", "gang", "overloop", "badkamer",
    "berging", "kast", "trap",
)
_OUT_OF_SCOPE_KEYWORDS = (
    "shaft", "riser", "chase", "elevator", "elev", "lift", "duct", "roof",
    "void", "open to below", "garage", "parking", "plant", "mechanical",
    "mech", "electrical", "elec", "server", "equip", "instal", "onben",
)


def _required_percent(space) -> float | None:
    text = " ".join(
        str(getattr(space, a, None) or "") for a in ("LongName", "Name")
    ).lower()
    if not text.strip() or any(k in text for k in _OUT_OF_SCOPE_KEYWORDS):
        return None
    if any(k in text for k in _KITCHEN_KEYWORDS):
        return KITCHEN_PERCENT
    if any(k in text for k in _NON_HABITABLE_KEYWORDS):
        return NON_HABITABLE_PERCENT
    if any(k in text for k in _HABITABLE_KEYWORDS):
        return HABITABLE_PERCENT
    return None


def _is_external(element) -> bool:
    for pdef in psets_of(element):
        if prop_value(pdef, "IsExternal") is True:
            return True
    return False


def _area_unit_to_m2(model: ifcopenshell.file) -> float:
    """The project's AREAUNIT in m2  - declared independently of LENGTHUNIT."""
    prefix = {"KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1,
              "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6}
    projects = model.by_type("IfcProject")
    units = getattr(projects[0].UnitsInContext, "Units", None) or () if projects else ()
    for unit in units:
        if getattr(unit, "UnitType", None) != "AREAUNIT":
            continue
        if unit.is_a("IfcSIUnit") and str(unit.Name).upper() == "SQUARE_METRE":
            return prefix.get(str(unit.Prefix).upper(), 1.0) ** 2 if unit.Prefix else 1.0
    scale = length_unit_scale(model)
    return (1.0 / (scale * 1000.0)) ** 2


def _space_area_m2(model: ifcopenshell.file, space, area_to_m2: float) -> float | None:
    best = None
    for rel in getattr(space, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        qset = rel.RelatingPropertyDefinition
        if qset is None or not qset.is_a("IfcElementQuantity"):
            continue
        for item in qset.Quantities or ():
            if not item.is_a("IfcQuantityArea") or not item.AreaValue:
                continue
            value = float(item.AreaValue) * area_to_m2
            if item.Name in ("NetFloorArea", "GrossFloorArea"):
                best = value
                break
            if best is None:
                best = value
    if best is not None and MIN_PLAUSIBLE_AREA_M2 <= best <= MAX_PLAUSIBLE_AREA_M2:
        return best
    return None


def _window_area_m2(model: ifcopenshell.file, window, scale: float) -> float | None:
    if window.OverallWidth is None or window.OverallHeight is None:
        return None
    width_mm = to_mm(model, float(window.OverallWidth), scale)
    height_mm = to_mm(model, float(window.OverallHeight), scale)
    return (width_mm / 1000.0) * (height_mm / 1000.0)


def _rooms(model: ifcopenshell.file):
    """window GlobalId -> the in-scope spaces its exterior host wall bounds,
    plus each space's floor area, required percent and total opening area."""
    scale = length_unit_scale(model)
    area_to_m2 = _area_unit_to_m2(model)

    wall_to_spaces: dict[int, list] = {}
    for rel in model.by_type("IfcRelSpaceBoundary"):
        element = rel.RelatedBuildingElement
        space = rel.RelatingSpace
        if element is None or space is None or not element.is_a("IfcWall"):
            continue
        wall_to_spaces.setdefault(element.id(), []).append(space)

    windows = sorted_by_guid(model.by_type("IfcWindow"))
    linked = 0
    window_spaces: dict[str, list] = {}
    opening_area: dict[int, float] = {}
    for window in windows:
        wall = get_host_wall(model, window)
        if wall is None:
            continue
        linked += 1
        if not _is_external(wall):
            continue
        area = _window_area_m2(model, window, scale)
        for space in wall_to_spaces.get(wall.id(), ()):
            if _required_percent(space) is None:
                continue
            window_spaces.setdefault(window.GlobalId, []).append(space)
            if area is not None:
                opening_area[space.id()] = opening_area.get(space.id(), 0.0) + area

    coverage = linked / len(windows) if windows else 0.0
    return window_spaces, opening_area, area_to_m2, coverage


def _usable(model: ifcopenshell.file, exclude=frozenset()):
    scale = length_unit_scale(model)
    window_spaces, opening_area, area_to_m2, coverage = _rooms(model)
    if coverage < MIN_LINK_COVERAGE:
        return [], coverage

    out = []
    for window in sorted_by_guid(model.by_type("IfcWindow")):
        if window.GlobalId in exclude:
            continue
        spaces = window_spaces.get(window.GlobalId)
        if not spaces:
            continue
        area = _window_area_m2(model, window, scale)
        if area is None:
            continue
        width_mm = to_mm(model, float(window.OverallWidth), scale)
        height_mm = to_mm(model, float(window.OverallHeight), scale)
        if min(width_mm, height_mm) <= MIN_WINDOW_DIMENSION_MM:
            continue

        new_width_mm = max(MIN_WINDOW_DIMENSION_MM, width_mm * SHRINK_FACTOR)
        new_height_mm = max(MIN_WINDOW_DIMENSION_MM, height_mm * SHRINK_FACTOR)
        lost_m2 = area - (new_width_mm / 1000.0) * (new_height_mm / 1000.0)

        for space in spaces:
            floor_area = _space_area_m2(model, space, area_to_m2)
            if floor_area is None:
                continue
            required = _required_percent(space)
            before = 100.0 * opening_area.get(space.id(), 0.0) / floor_area
            after = 100.0 * (opening_area.get(space.id(), 0.0) - lost_m2) / floor_area
            # Only worth offering if this one window is what tips the room
            # over: the checker has to be able to find the defect.
            if before >= required > after:
                out.append((window, space, width_mm, height_mm, new_width_mm,
                            new_height_mm, before, after, required, floor_area))
                break
    return out, coverage


def applicable(model: ifcopenshell.file) -> Applicability:
    usable, coverage = _usable(model)
    windows = len(model.by_type("IfcWindow"))
    if not windows:
        return Applicability(False, "no IfcWindow in the model")
    if coverage < MIN_LINK_COVERAGE:
        return Applicability(
            False,
            f"only {coverage:.0%} of the model's {windows} window(s) resolve to a host wall, "
            f"below the {MIN_LINK_COVERAGE:.0%} a room-by-room opening ratio needs",
        )
    if not usable:
        return Applicability(
            False,
            f"no single window whose shrinking would drop its room below the Table 3.1.12 "
            f"percentage ({windows} IfcWindow total)",
        )
    return Applicability(
        True,
        f"{len(usable)} window(s) whose shrinking drops their room below the required "
        f"opening percentage",
    )


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    usable, _coverage = _usable(model, exclude)
    if not usable:
        return []

    out = []
    for (window, space, width_mm, height_mm, new_width_mm, new_height_mm,
         before, after, required, floor_area) in usable:
        out.append(ScoredTarget(
            global_id=window.GlobalId,
            # The deeper the room falls below the limit, the less ambiguous
            # the resulting defect.
            score=required - after,
            justification=(
                f"window {width_mm:.0f}x{height_mm:.0f}mm serving "
                f"'{space.LongName or space.Name}' ({floor_area:.1f} m2, needs {required:.0f}%); "
                f"shrinking it takes that room from {before:.1f}% to {after:.1f}%"
            ),
            element_ids=(window.id(),),
            extra={
                "before_width_mm": width_mm,
                "before_height_mm": height_mm,
                "new_width_mm": new_width_mm,
                "new_height_mm": new_height_mm,
                "space_global_id": space.GlobalId,
                "space_name": space.LongName or space.Name,
                "space_floor_area_m2": floor_area,
                "required_percent": required,
                "before_percent": before,
                "after_percent": after,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    window = model.by_guid(target.global_id)

    new_width_mm = float(params.get("new_width_mm", target.extra["new_width_mm"]))
    new_height_mm = float(params.get("new_height_mm", target.extra["new_height_mm"]))

    before_width_mm = target.extra["before_width_mm"]
    before_height_mm = target.extra["before_height_mm"]
    window.OverallWidth = mm(model, new_width_mm, scale)
    window.OverallHeight = mm(model, new_height_mm, scale)

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcWindow",
        target_global_id=target.global_id,
        attribute="IfcWindow.OverallWidth / OverallHeight",
        before=before_width_mm * before_height_mm / 1e6,
        after=new_width_mm * new_height_mm / 1e6,
        clause=CLAUSE,
        description=(
            f"Window shrunk from {before_width_mm:.0f}x{before_height_mm:.0f}mm to "
            f"{new_width_mm:.0f}x{new_height_mm:.0f}mm, taking the opening area of room "
            f"'{target.extra['space_name']}' from {target.extra['before_percent']:.1f}% to "
            f"{target.extra['after_percent']:.1f}% of its "
            f"{target.extra['space_floor_area_m2']:.1f} m2 floor area, below the "
            f"{target.extra['required_percent']:.0f}% Table 3.1.12 minimum"
        ),
        extra={
            "element_id": window.id(),
            "before_width_mm": before_width_mm,
            "before_height_mm": before_height_mm,
            "after_width_mm": new_width_mm,
            "after_height_mm": new_height_mm,
            "space_global_id": target.extra["space_global_id"],
            "space_name": target.extra["space_name"],
            "space_floor_area_m2": target.extra["space_floor_area_m2"],
            "required_percent": target.extra["required_percent"],
            "before_percent": target.extra["before_percent"],
            "after_percent": target.extra["after_percent"],
            "mechanism": "direct attribute write",
        },
    )
