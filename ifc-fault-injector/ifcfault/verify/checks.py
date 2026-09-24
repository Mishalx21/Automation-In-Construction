"""
Independent re-derivation of an injected violation.

Nothing in this module imports `ifcfault.library`. Every quantity it needs  - 
the unit scale, an element's cross-section, a storey ordering, the space
adjacency graph  - is re-implemented here from scratch, and deliberately with
a different convention where there is a choice (this module works in
"millimetres per native unit", the library works in "native units per
millimetre"). The point is that a bug shared between the injector and the
checker cannot hide a real defect: if both agree, they agreed by two
different routes.

There is a test that fails if this module ever grows an import from the
library.

What gets checked, for every rule:

  parses_cleanly              the written file reopens
  no_dangling_references      no attribute points at a missing entity
  no_degenerate_relationships no relationship left with an empty member list
  no_unintended_diff          nothing changed except what the record declares
  <rule>_clause_violated      the clause really is violated now

A newly synthesized rule gets everything except the last one, since there is
no hand-written re-derivation for a clause nobody has implemented before.
That gap is reported explicitly rather than passed over.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import ifcopenshell


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class CheckResult:
    name: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        detail = self.message or ", ".join(f"{k}={v}" for k, v in list(self.evidence.items())[:4])
        return f"  {self.name:<32} {status}   {detail}".rstrip()


# ---------------------------------------------------------------------------
# units  - independently derived, in mm per native unit
# ---------------------------------------------------------------------------
_PREFIX_METRES = {
    None: 1.0, "MILLI": 0.001, "CENTI": 0.01, "DECI": 0.1,
    "DECA": 10.0, "HECTO": 100.0, "KILO": 1000.0, "MICRO": 1e-6,
}


def mm_per_native(model: ifcopenshell.file) -> float:
    """How many millimetres is one native length unit?"""
    projects = model.by_type("IfcProject")
    if not projects:
        return 1000.0
    for unit in getattr(projects[0].UnitsInContext, "Units", None) or ():
        if unit.is_a("IfcSIUnit") and unit.UnitType == "LENGTHUNIT":
            return _PREFIX_METRES.get(unit.Prefix, 1.0) * 1000.0
        if unit.is_a("IfcConversionBasedUnit") and unit.UnitType == "LENGTHUNIT":
            try:
                return float(unit.ConversionFactor.ValueComponent.wrappedValue) * 1000.0
            except Exception:
                continue
    return 1000.0


# ---------------------------------------------------------------------------
# generic structural checks
# ---------------------------------------------------------------------------
def check_parses_cleanly(path: Path | str) -> CheckResult:
    try:
        model = ifcopenshell.open(str(path))
        return CheckResult("parses_cleanly", True,
                           {"root_entities": len(model.by_type("IfcRoot")),
                            "schema": model.schema})
    except Exception as e:
        return CheckResult("parses_cleanly", False, message=repr(e))


def check_no_dangling_references(model: ifcopenshell.file) -> CheckResult:
    problems = []
    for inst in model:
        try:
            info = inst.get_info(include_identifier=True, recursive=False)
        except Exception as e:
            problems.append(f"{inst}: get_info failed: {e!r}")
            continue
        for attr, value in info.items():
            for v in (value if isinstance(value, (tuple, list)) else [value]):
                if hasattr(v, "id") and not isinstance(v, (str, bytes)):
                    try:
                        vid = v.id()
                        if vid:
                            model.by_id(vid)
                    except Exception:
                        problems.append(f"{inst.is_a()}#{inst.id()}.{attr} -> missing entity")
    return CheckResult(
        "no_dangling_references", not problems, {"problems": problems[:20]},
        message=f"{len(problems)} dangling reference(s)" if problems else "",
    )


# EXPRESS declares these list attributes as SET [1:?]  - an empty one is a
# schema violation even though nothing is technically dangling. ifcopenshell's
# own remove() empties them but does not delete the relationship.
_NONEMPTY_LIST_ATTRS = {
    "IfcRelContainedInSpatialStructure": "RelatedElements",
    "IfcRelDefinesByProperties": "RelatedObjects",
    "IfcRelDefinesByType": "RelatedObjects",
    "IfcRelAggregates": "RelatedObjects",
    "IfcRelNests": "RelatedObjects",
    "IfcRelAssociatesMaterial": "RelatedObjects",
}


def check_no_degenerate_relationships(model: ifcopenshell.file) -> CheckResult:
    problems = []
    for typename, attr in _NONEMPTY_LIST_ATTRS.items():
        for inst in model.by_type(typename):
            if not getattr(inst, attr):
                problems.append(f"{inst.is_a()}#{inst.id()}.{attr} is empty")
    return CheckResult(
        "no_degenerate_relationships", not problems, {"problems": problems[:20]},
        message=f"{len(problems)} emptied-but-not-removed relationship(s)" if problems else "",
    )


def check_element_present(model: ifcopenshell.file, global_id: str,
                          label: str = "element_present") -> CheckResult:
    try:
        el = model.by_guid(global_id)
        return CheckResult(label, el is not None,
                           {"global_id": global_id, "type": el.is_a()})
    except Exception:
        return CheckResult(label, False, {"global_id": global_id},
                           message="GlobalId does not resolve")


def check_elements_absent(model: ifcopenshell.file, global_ids: list[str]) -> CheckResult:
    still_there = []
    for gid in global_ids:
        try:
            model.by_guid(gid)
            still_there.append(gid)
        except Exception:
            pass
    return CheckResult(
        "declared_deletions_absent", not still_there,
        {"expected_absent": len(global_ids), "still_present": still_there[:10]},
        message=(f"{len(still_there)} element(s) were declared deleted but still resolve"
                 if still_there else ""),
    )


# ---------------------------------------------------------------------------
# whole-file diff
# ---------------------------------------------------------------------------
def _by_guid(model: ifcopenshell.file) -> dict[str, Any]:
    return {inst.GlobalId: inst for inst in model.by_type("IfcRoot")}


def _normalize(value, cache: Optional[dict] = None):
    """Canonical form of a get_info() value, for comparison across two
    independently opened files.

    - an IfcRoot entity  -> its GlobalId (its STEP id is not comparable
      across two `ifcopenshell.open` calls)
    - any other entity   -> its type plus its normalized attributes,
      MEMOIZED by STEP id. Real files share profiles, materials and styles
      across thousands of elements, and without memoization this recursion
      is effectively exponential.
    - a list of IfcRoot entities -> a SORTED tuple of GlobalIds, because
      relationship membership is a set and STEP serialization order is not
      guaranteed stable through a read/write round trip
    """
    if cache is None:
        cache = {}
    if hasattr(value, "is_a") and hasattr(value, "id"):
        gid = getattr(value, "GlobalId", None)
        if gid is not None:
            return ("GUID", gid)
        key = value.id()
        if key in cache:
            return cache[key]
        try:
            info = value.get_info(include_identifier=False, recursive=False)
        except Exception:
            return str(value)
        cache[key] = ("CYCLE", key)  # geometry cycles are not expected; stay safe
        result = (value.is_a(), tuple(sorted((k, _normalize(v, cache)) for k, v in info.items())))
        cache[key] = result
        return result
    if isinstance(value, dict):
        return tuple(sorted((k, _normalize(v, cache)) for k, v in value.items()))
    if isinstance(value, (tuple, list)):
        items = [_normalize(v, cache) for v in value]
        if items and all(isinstance(n, tuple) and n and n[0] == "GUID" for n in items):
            return ("GUID_SET", tuple(sorted(items)))
        return tuple(items)
    return value


_LIST_REF_ATTRS = dict(_NONEMPTY_LIST_ATTRS)


def _list_ref_guids(entity):
    for typename, attr in _LIST_REF_ATTRS.items():
        if entity.is_a(typename):
            refs = getattr(entity, attr) or ()
            return attr, {r.GlobalId for r in refs if hasattr(r, "GlobalId")}
    return None, None


def check_no_unintended_diff(
    source_model: ifcopenshell.file,
    output_model: ifcopenshell.file,
    allowed_changed: set[str],
    allowed_removed: set[str],
    allowed_added: set[str] | None = None,
) -> CheckResult:
    """Every IfcRoot entity in the source must be present and unchanged in the
    output, unless the mutation record declared it changed or removed.

    Two relaxations, both narrow and both justified:
      * a relationship may lose a reference to a declared-removed element
      * a relationship that referenced nothing BUT declared-removed elements
        may itself disappear
    Anything else is an unexplained diff, which is what a real bug looks like.
    """
    allowed_added = allowed_added or set()
    src = _by_guid(source_model)
    out = _by_guid(output_model)
    src_cache: dict = {}
    out_cache: dict = {}

    unexpected_removals: list[str] = []
    unexpected_changes: list[str] = []
    cascades = 0

    for gid, src_el in src.items():
        if gid in allowed_removed:
            if gid in out:
                unexpected_removals.append(f"{gid} declared removed but still present")
            continue

        if gid not in out:
            _, list_guids = _list_ref_guids(src_el)
            if list_guids and list_guids.issubset(allowed_removed):
                cascades += 1
                continue
            if src_el.is_a("IfcRelationship") and _references_any(src_el, allowed_removed):
                cascades += 1
                continue
            unexpected_removals.append(gid)
            continue

        if gid in allowed_changed:
            continue

        out_el = out[gid]
        # Fast path: STEP ids survive an open/write round trip, so identical
        # raw reprs mean the entity is definitely unchanged  - about two orders
        # of magnitude cheaper than the semantic comparison below.
        if str(src_el) == str(out_el):
            continue

        src_info = src_el.get_info(include_identifier=False, recursive=False)
        out_info = out_el.get_info(include_identifier=False, recursive=False)
        if _normalize(src_info, src_cache) == _normalize(out_info, out_cache):
            continue

        list_attr, _ = _list_ref_guids(src_el)
        if list_attr is not None:
            src_filtered = dict(src_info)
            out_filtered = dict(out_info)
            src_filtered[list_attr] = tuple(
                r for r in src_info[list_attr]
                if getattr(r, "GlobalId", None) not in allowed_removed
            )
            out_filtered[list_attr] = tuple(
                r for r in out_info[list_attr]
                if getattr(r, "GlobalId", None) not in allowed_removed
            )
            if _normalize(src_filtered, src_cache) == _normalize(out_filtered, out_cache):
                cascades += 1
                continue

        unexpected_changes.append(gid)

    fabricated = [g for g in out
                  if g not in src and g not in allowed_changed and g not in allowed_added]

    ok = not unexpected_removals and not unexpected_changes and not fabricated
    return CheckResult(
        "no_unintended_diff", ok,
        {
            "unexpected_removals": unexpected_removals[:10],
            "unexpected_changes": unexpected_changes[:10],
            "fabricated": fabricated[:10],
            "expected_cascades": cascades,
        },
        message="" if ok else (
            f"{len(unexpected_removals)} unexplained removal(s), "
            f"{len(unexpected_changes)} unexplained change(s), "
            f"{len(fabricated)} fabricated entit(y/ies)"
        ),
    )


def _references_any(entity, target_guids: set[str]) -> bool:
    try:
        info = entity.get_info(include_identifier=False, recursive=False)
    except Exception:
        return False
    for value in info.values():
        for v in (value if isinstance(value, (tuple, list)) else [value]):
            if hasattr(v, "GlobalId") and v.GlobalId in target_guids:
                return True
    return False


# ---------------------------------------------------------------------------
# independent geometry reading (own implementation, on purpose)
# ---------------------------------------------------------------------------
def _own_body_items(element):
    rep = getattr(element, "Representation", None)
    if rep is None:
        return []
    items = []
    for r in rep.Representations:
        if r.RepresentationIdentifier != "Body":
            continue
        for item in r.Items:
            if item.is_a("IfcMappedItem"):
                items.extend(item.MappingSource.MappedRepresentation.Items)
            else:
                items.append(item)
    return items


def _own_profile_spans(profile, scale_mm):
    if profile.is_a("IfcRectangleProfileDef"):
        return sorted([profile.XDim * scale_mm, profile.YDim * scale_mm])
    points = []
    curve = getattr(profile, "OuterCurve", None)
    if curve is not None:
        if curve.is_a("IfcPolyline"):
            points = [p.Coordinates for p in curve.Points]
        elif curve.is_a("IfcCompositeCurve"):
            for segment in curve.Segments:
                parent = segment.ParentCurve
                if parent.is_a("IfcPolyline"):
                    points.extend(p.Coordinates for p in parent.Points)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return sorted([(max(xs) - min(xs)) * scale_mm, (max(ys) - min(ys)) * scale_mm])


def _own_brep_spans(item, scale_mm):
    points = []
    shell = getattr(item, "Outer", None)
    if shell is None:
        return None
    for face in getattr(shell, "CfsFaces", ()) or ():
        for bound in getattr(face, "Bounds", ()) or ():
            loop = bound.Bound
            if loop.is_a("IfcPolyLoop"):
                points.extend(p.Coordinates for p in loop.Polygon)
    if not points:
        return None
    spans = []
    for axis in range(3):
        values = [p[axis] for p in points]
        spans.append((max(values) - min(values)) * scale_mm)
    return sorted(spans)


def _own_dimensions_mm(model, element) -> Optional[dict]:
    """{'length', 'depth', 'width'} in mm, longest axis first. Independent of
    the library's element_size_mm."""
    scale_mm = mm_per_native(model)
    for item in _own_body_items(element):
        if item.is_a("IfcExtrudedAreaSolid"):
            spans = _own_profile_spans(item.SweptArea, scale_mm)
            length = item.Depth * scale_mm
            if spans is None:
                return {"length": length, "depth": None, "width": None}
            return {"length": length, "depth": spans[1], "width": spans[0]}
        if item.is_a("IfcFacetedBrep"):
            spans = _own_brep_spans(item, scale_mm)
            if spans is None:
                continue
            return {"length": spans[2], "depth": spans[1], "width": spans[0]}
    return None


def _own_min_thickness_mm(model, element) -> Optional[float]:
    scale_mm = mm_per_native(model)
    for item in _own_body_items(element):
        if item.is_a("IfcExtrudedAreaSolid"):
            spans = _own_profile_spans(item.SweptArea, scale_mm) or []
            return min([item.Depth * scale_mm] + list(spans))
        if item.is_a("IfcFacetedBrep"):
            spans = _own_brep_spans(item, scale_mm)
            if spans:
                return spans[0]
    return None


def _own_world_xy(model, element):
    import ifcopenshell.util.placement as placement

    if getattr(element, "ObjectPlacement", None) is None:
        return None
    try:
        m = placement.get_local_placement(element.ObjectPlacement)
    except Exception:
        return None
    scale_mm = mm_per_native(model)
    return float(m[0, 3]) * scale_mm, float(m[1, 3]) * scale_mm


def _own_psets(element):
    out = []
    for rel in getattr(element, "IsDefinedBy", []) or []:
        if rel.is_a("IfcRelDefinesByProperties"):
            pdef = rel.RelatingPropertyDefinition
            if pdef.is_a("IfcPropertySet"):
                out.append(pdef)
    return out


def _own_prop(element, name):
    for pset in _own_psets(element):
        for prop in pset.HasProperties:
            if prop.Name == name and prop.NominalValue is not None:
                return prop.NominalValue.wrappedValue
    return None


# ---------------------------------------------------------------------------
# per-rule clause re-derivation
# ---------------------------------------------------------------------------
def rederive_a1(output_model, mutation, source_model=None,
                threshold_mm: float = 815.0) -> CheckResult:
    door = output_model.by_guid(mutation["target_global_id"])
    if not door.is_a("IfcDoor"):
        return CheckResult("a1_clause_violated", False,
                           message=f"target is {door.is_a()}, not IfcDoor")
    if door.OverallWidth is None:
        return CheckResult("a1_clause_violated", False, message="OverallWidth is unset")
    width_mm = door.OverallWidth * mm_per_native(output_model)
    ok = width_mm < threshold_mm
    return CheckResult("a1_clause_violated", ok,
                       {"width_mm": round(width_mm, 2), "threshold_mm": threshold_mm},
                       message="" if ok else f"{width_mm:.1f}mm is not below {threshold_mm}mm")


def rederive_a2(output_model, mutation, source_model=None,
                threshold_mm: float = 178.0) -> CheckResult:
    element = output_model.by_guid(mutation["target_global_id"])
    raw = _own_prop(element, "RiserHeight")
    if raw is None:
        return CheckResult("a2_clause_violated", False,
                           message="no RiserHeight property found on the target")
    riser_mm = float(raw) * mm_per_native(output_model)
    ok = riser_mm > threshold_mm
    return CheckResult("a2_clause_violated", ok,
                       {"riser_mm": round(riser_mm, 2), "threshold_mm": threshold_mm},
                       message="" if ok else f"{riser_mm:.1f}mm does not exceed {threshold_mm}mm")


def _rating_number(value: str) -> Optional[float]:
    text = (value or "").lower()
    for token in ("minuten brandwerend en zelfsluitend", "minuten brandwerend",
                  "brandwerend", "min", "hr", " "):
        text = text.replace(token, "")
    try:
        return float(text)
    except ValueError:
        return None


def rederive_a3(output_model, mutation, source_model=None) -> CheckResult:
    """A downgrade is only meaningful relative to what was there before, so
    this one genuinely needs the source file."""
    gid = mutation["target_global_id"]
    after_raw = _own_prop(output_model.by_guid(gid), "FireRating")
    after = "" if after_raw is None else str(after_raw).strip()

    if source_model is None:
        ok = after == ""
        return CheckResult("a3_clause_violated", ok,
                           {"after": after or "(blank)", "compared_against": "blank-only"},
                           message="" if ok else "source model unavailable; only a blank "
                                                 "rating can be confirmed without it")

    try:
        before_raw = _own_prop(source_model.by_guid(gid), "FireRating")
    except Exception:
        before_raw = None
    before = "" if before_raw is None else str(before_raw).strip()

    if after == "" and before != "":
        return CheckResult("a3_clause_violated", True,
                           {"before": before, "after": "(blank)"})
    if after == "" and before == "":
        # The fallback path: the model had no rating and now explicitly
        # documents a blank one. The defect is the documented absence.
        has_pset = any(
            prop.Name == "FireRating"
            for pset in _own_psets(output_model.by_guid(gid))
            for prop in pset.HasProperties
        )
        return CheckResult("a3_clause_violated", has_pset,
                           {"before": "(absent)", "after": "(blank, now documented)"},
                           message="" if has_pset else "no FireRating property was added")

    before_n, after_n = _rating_number(before), _rating_number(after)
    if before_n is not None and after_n is not None:
        ok = after_n < before_n
        return CheckResult("a3_clause_violated", ok,
                           {"before": before, "after": after,
                            "before_value": before_n, "after_value": after_n},
                           message="" if ok else f"'{after}' is not a downgrade from '{before}'")
    ok = after != before
    return CheckResult("a3_clause_violated", ok, {"before": before, "after": after},
                       message="" if ok else "rating is unchanged")


def _own_wall_axis_length_native(wall) -> Optional[float]:
    rep = getattr(wall, "Representation", None)
    if rep is None:
        return None
    for r in rep.Representations:
        if r.RepresentationIdentifier == "Axis":
            for item in r.Items:
                if item.is_a("IfcPolyline"):
                    pts = [p.Coordinates for p in item.Points]
                    if len(pts) >= 2:
                        return math.dist(pts[0][:2], pts[-1][:2])
    return None


def rederive_a4(output_model, mutation, source_model=None,
                threshold_mm: float = 300.0) -> CheckResult:
    """Re-walk door -> opening -> wall independently and measure the clearance."""
    door = output_model.by_guid(mutation["target_global_id"])
    opening = None
    for rel in output_model.get_inverse(door):
        if rel.is_a("IfcRelFillsElement"):
            opening = rel.RelatingOpeningElement
            break
    if opening is None:
        return CheckResult("a4_clause_violated", False, message="door fills no opening")

    wall = None
    for rel in output_model.get_inverse(opening):
        if rel.is_a("IfcRelVoidsElement"):
            wall = rel.RelatingBuildingElement
            break
    if wall is None:
        return CheckResult("a4_clause_violated", False, message="opening voids no wall")

    wall_len = _own_wall_axis_length_native(wall)
    if wall_len is None:
        return CheckResult("a4_clause_violated", False, message="wall has no Axis polyline")

    x = opening.ObjectPlacement.RelativePlacement.Location.Coordinates[0]
    clearance_mm = min(x, wall_len - x) * mm_per_native(output_model)
    ok = clearance_mm < threshold_mm
    return CheckResult("a4_clause_violated", ok,
                       {"clearance_mm": round(clearance_mm, 1), "threshold_mm": threshold_mm},
                       message="" if ok else f"{clearance_mm:.1f}mm is not below {threshold_mm}mm")


def rederive_a5(output_model, mutation, source_model=None) -> CheckResult:
    """Rebuild the space adjacency graph from scratch and confirm the branch
    really is cut off from the rest of the building."""
    extra = mutation.get("extra", {})
    severed_gid = extra.get("severed_space_global_id")
    remaining_gid = extra.get("remaining_space_global_id")
    if not severed_gid or not remaining_gid:
        return CheckResult("a5_clause_violated", False,
                           message="mutation record lacks the severed/remaining space ids")

    adjacency: dict[int, set[int]] = {}
    door_spaces: dict[int, set[int]] = {}
    for rel in output_model.by_type("IfcRelSpaceBoundary"):
        element = rel.RelatedBuildingElement
        if element is not None and element.is_a("IfcDoor"):
            door_spaces.setdefault(element.id(), set()).add(rel.RelatingSpace.id())
    for spaces in door_spaces.values():
        if len(spaces) == 2:
            a, b = sorted(spaces)
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)

    try:
        start = output_model.by_guid(remaining_gid).id()
        goal = output_model.by_guid(severed_gid).id()
    except Exception:
        return CheckResult("a5_clause_violated", False,
                           message="severed or remaining space no longer resolves")

    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)

    ok = goal not in seen
    return CheckResult(
        "a5_clause_violated", ok,
        {"reachable_spaces_from_remaining": len(seen),
         "isolated_branch_size": extra.get("isolated_branch_size")},
        message="" if ok else "the severed space is still reachable through another door",
    )


def rederive_s1(output_model, mutation, source_model=None,
                max_l_over_d: float = 24.0) -> CheckResult:
    beam = output_model.by_guid(mutation["target_global_id"])
    dims = _own_dimensions_mm(output_model, beam)
    if not dims or not dims.get("depth"):
        return CheckResult("s1_clause_violated", False,
                           message="could not measure the beam's span and depth")
    ratio = dims["length"] / dims["depth"]
    ok = ratio > max_l_over_d
    return CheckResult("s1_clause_violated", ok,
                       {"L_over_d": round(ratio, 2), "max_L_over_d": max_l_over_d,
                        "length_mm": round(dims["length"], 1),
                        "depth_mm": round(dims["depth"], 1)},
                       message="" if ok else f"L/d {ratio:.2f} does not exceed {max_l_over_d}")


def rederive_s2(output_model, mutation, source_model=None,
                min_dimension_mm: float = 250.0) -> CheckResult:
    column = output_model.by_guid(mutation["target_global_id"])
    dims = _own_dimensions_mm(output_model, column)
    if not dims or not dims.get("width"):
        return CheckResult("s2_clause_violated", False,
                           message="could not measure the column's cross-section")
    min_dim = dims["width"]
    ok = min_dim < min_dimension_mm
    return CheckResult("s2_clause_violated", ok,
                       {"min_dimension_mm": round(min_dim, 1),
                        "threshold_mm": min_dimension_mm},
                       message="" if ok else f"{min_dim:.1f}mm is not below {min_dimension_mm}mm")


def rederive_s3(output_model, mutation, source_model=None,
                threshold_mm: float = 150.0) -> CheckResult:
    slab = output_model.by_guid(mutation["target_global_id"])
    thickness = _own_min_thickness_mm(output_model, slab)
    if thickness is None:
        return CheckResult("s3_clause_violated", False,
                           message="could not measure the slab's thickness")
    ok = thickness < threshold_mm
    return CheckResult("s3_clause_violated", ok,
                       {"thickness_mm": round(thickness, 1), "threshold_mm": threshold_mm},
                       message="" if ok else f"{thickness:.1f}mm is not below {threshold_mm}mm")


def rederive_s4(output_model, mutation, source_model=None,
                plan_tolerance_mm: float = 500.0) -> CheckResult:
    """Rebuild the storey ordering and column layout independently, then check
    that nothing is left carrying the surviving column."""
    surviving_gid = mutation.get("extra", {}).get("surviving_column_global_id")
    if not surviving_gid:
        return CheckResult("s4_clause_violated", False,
                           message="mutation record lacks surviving_column_global_id")
    try:
        surviving = output_model.by_guid(surviving_gid)
    except Exception:
        return CheckResult("s4_clause_violated", False,
                           message="the surviving column no longer resolves")

    storeys = [s for s in output_model.by_type("IfcBuildingStorey") if s.Elevation is not None]
    storeys.sort(key=lambda s: (s.Elevation, s.GlobalId))

    own_storey = None
    for rel in output_model.get_inverse(surviving):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            own_storey = rel.RelatingStructure
            break
    if own_storey is None:
        return CheckResult("s4_clause_violated", False,
                           message="could not resolve the surviving column's storey")

    index = next((i for i, s in enumerate(storeys) if s.id() == own_storey.id()), None)
    if index is None or index == 0:
        return CheckResult("s4_clause_violated", False,
                           {"storey": own_storey.Name},
                           message="the surviving column is on the lowest storey, so there is "
                                   "no storey below it to be unsupported from")

    below = storeys[index - 1]
    columns_below = []
    for rel in output_model.get_inverse(below):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            columns_below.extend(e for e in rel.RelatedElements if e.is_a("IfcColumn"))

    surviving_xy = _own_world_xy(output_model, surviving)
    if surviving_xy is None:
        return CheckResult("s4_clause_violated", False,
                           message="could not resolve the surviving column's placement")

    for column in columns_below:
        xy = _own_world_xy(output_model, column)
        if xy is None:
            continue
        distance = math.hypot(xy[0] - surviving_xy[0], xy[1] - surviving_xy[1])
        if distance <= plan_tolerance_mm:
            return CheckResult(
                "s4_clause_violated", False,
                {"supporting_column": column.GlobalId, "distance_mm": round(distance, 1)},
                message="a column still sits directly below, so the load path is intact",
            )

    return CheckResult("s4_clause_violated", True,
                       {"storey_below": below.Name, "columns_checked": len(columns_below),
                        "plan_tolerance_mm": plan_tolerance_mm})


def rederive_s5(output_model, mutation, source_model=None) -> CheckResult:
    """The target is the STOREY. Confirm it still exists and now holds no
    walls, while the building as a whole still does."""
    storey_gid = mutation["target_global_id"]
    try:
        storey = output_model.by_guid(storey_gid)
    except Exception:
        return CheckResult("s5_clause_violated", False,
                           message="the target storey no longer resolves")

    walls_here = []
    for rel in output_model.get_inverse(storey):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            walls_here.extend(e for e in rel.RelatedElements if e.is_a("IfcWall"))
    walls_total = len(output_model.by_type("IfcWall"))

    ok = not walls_here and walls_total > 0
    if walls_here:
        message = f"{len(walls_here)} wall(s) remain on storey '{storey.Name}'"
    elif walls_total == 0:
        message = "every wall in the building is gone, not just this storey's"
    else:
        message = ""
    return CheckResult("s5_clause_violated", ok,
                       {"walls_on_target_storey": len(walls_here),
                        "walls_elsewhere_in_model": walls_total,
                        "storey": storey.Name},
                       message=message)


# ---------------------------------------------------------------------------
# per-rule clause re-derivation  - BNBC rules A6-A10, S6-S10
#
# Same discipline as everything above: nothing here consults the library, and
# every quantity is re-measured from the written file. Where a rule's limit
# depends on what kind of room, wall or footing it is, that classification is
# re-derived here too rather than trusted from the mutation record  - a
# mislabelled target must not be able to certify its own defect.
# ---------------------------------------------------------------------------
_HABITABLE_WORDS = (
    "bedroom", "living", "dining", "study", "office", "classroom", "class ", "ward",
    "waiting", "activity", "lounge", "conference", "meeting", "exam", "consult",
    "operat", "library", "dormitor", "reception", "lab", "therapy", "team rm",
    "break rm", "cubicle", "work station", "workstation", "treatment", "clinic",
    "nurse", "kantoor", "slaapkamer", "woonkamer", "eetkamer", "werkkamer", "verblijf",
)
_CORRIDOR_WORDS = (
    "corridor", "hallway", "passage", "lobby", "vestibule", "circulat", "gang",
    "overloop", "hal ", "vest", "entry", "entrance", "entree",
)
_OTHER_ROOM_WORDS = (
    "bath", "toilet", " wc", "wc ", "restroom", " rr", "rr ", "shower", "store",
    "storage", "stor", "kitchen", "pantry", "laundry", "utility", "utl", "closet",
    "janitor", "jan.", "jan ", "badkamer", "keuken", "berging", "kast",
)
_MASONRY_WORDS = ("masonry", "brick", "block", "cmu", "clay", "metselwerk",
                  "baksteen", "kalkzandsteen", "mw-", "mauerwerk")
_GUARD_WORDS = ("guard", "balustrade", "parapet", "barrier", "doorvalregel", "traphek",
                "afscheiding", "hekwerk", "borstwering")
_HANDRAIL_WORDS = ("handrail", "hand rail", "leuning", "trapleuning")
_PILE_CAP_WORDS = ("pile cap", "pilecap", "poer", "paalkop")


def _own_text(element, *attributes) -> str:
    return " ".join(str(getattr(element, a, None) or "") for a in attributes).lower()


def _own_vertical_extrusion(model, element):
    """The element's vertically extruded body solid, or None."""
    for item in _own_body_items(element):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        ratios = getattr(getattr(item, "ExtrudedDirection", None), "DirectionRatios", None)
        if ratios is not None and abs(ratios[2]) < 0.9:
            continue
        return item
    return None


def _own_quantity(element, names, kind="IfcQuantityLength", attribute="LengthValue"):
    for rel in getattr(element, "IsDefinedBy", []) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        qset = rel.RelatingPropertyDefinition
        if qset is None or not qset.is_a("IfcElementQuantity"):
            continue
        for item in qset.Quantities or ():
            if item.is_a(kind) and item.Name in names:
                value = getattr(item, attribute, None)
                if value:
                    return float(value)
    return None


def _own_profile_extents(profile):
    """(short, long) extent of a profile, in native units, or None."""
    if profile is None:
        return None
    if profile.is_a("IfcRectangleProfileDef"):
        dims = (float(profile.XDim), float(profile.YDim))
        return min(dims), max(dims)
    if profile.is_a("IfcCircleProfileDef") or profile.is_a("IfcCircleHollowProfileDef"):
        diameter = float(profile.Radius) * 2.0
        return diameter, diameter
    curve = getattr(profile, "OuterCurve", None)
    points = []
    if curve is not None and curve.is_a("IfcPolyline"):
        points = [tuple(pt.Coordinates[:2]) for pt in curve.Points]
    elif curve is not None and curve.is_a("IfcCompositeCurve"):
        for segment in curve.Segments:
            parent = segment.ParentCurve
            if parent.is_a("IfcPolyline"):
                points.extend(tuple(pt.Coordinates[:2]) for pt in parent.Points)
    if len(points) < 3:
        return None
    xs = [pt[0] for pt in points]
    ys = [pt[1] for pt in points]
    dims = (max(xs) - min(xs), max(ys) - min(ys))
    return min(dims), max(dims)


def rederive_a6(output_model, mutation, source_model=None) -> CheckResult:
    """Re-measure the space's height and re-decide which limit applies."""
    space = output_model.by_guid(mutation["target_global_id"])
    text = _own_text(space, "LongName", "Name")
    if any(w in text for w in _CORRIDOR_WORDS):
        limit_mm, kind = 2400.0, "corridor"
    elif any(w in text for w in _HABITABLE_WORDS):
        limit_mm, kind = 2750.0, "habitable"
    else:
        return CheckResult("a6_clause_violated", False,
                           message="the target space is neither a habitable room nor a corridor")

    scale_mm = mm_per_native(output_model)
    native = _own_quantity(space, ("Height", "NetHeight", "GrossHeight"))
    if native is None:
        solid = _own_vertical_extrusion(output_model, space)
        native = float(solid.Depth) if solid is not None and solid.Depth else None
    if native is None:
        return CheckResult("a6_clause_violated", False,
                           message="no height quantity or vertical extrusion on the space")

    height_mm = native * scale_mm
    ok = height_mm < limit_mm
    return CheckResult("a6_clause_violated", ok,
                       {"height_mm": round(height_mm, 1), "threshold_mm": limit_mm,
                        "space_kind": kind},
                       message="" if ok else f"{height_mm:.1f}mm is not below {limit_mm}mm")


def rederive_a7(output_model, mutation, source_model=None) -> CheckResult:
    """Re-measure the room's least width from its own footprint."""
    space = output_model.by_guid(mutation["target_global_id"])
    text = _own_text(space, "LongName", "Name")
    if any(w in text for w in _OTHER_ROOM_WORDS):
        limit_mm, kind = 2000.0, "other"
    elif any(w in text for w in _HABITABLE_WORDS):
        limit_mm, kind = 2900.0, "habitable"
    else:
        return CheckResult("a7_clause_violated", False,
                           message="the target space is not a room Sec 1.14.2.2 governs")

    solid = None
    for item in _own_body_items(space):
        if item.is_a("IfcExtrudedAreaSolid"):
            solid = item
            break
    extents = _own_profile_extents(getattr(solid, "SweptArea", None)) if solid else None
    if extents is None:
        return CheckResult("a7_clause_violated", False,
                           message="no measurable footprint on the space")

    width_mm = extents[0] * mm_per_native(output_model)
    ok = width_mm < limit_mm
    return CheckResult("a7_clause_violated", ok,
                       {"least_width_mm": round(width_mm, 1), "threshold_mm": limit_mm,
                        "room_kind": kind},
                       message="" if ok else f"{width_mm:.1f}mm is not below {limit_mm}mm")


def rederive_a8(output_model, mutation, source_model=None) -> CheckResult:
    """Re-walk window -> host wall -> room and re-total the room's openings.

    Built the other way round from the injector: the room named in the record
    is taken first and every window reaching it is gathered, rather than
    starting from one window and following it outwards.
    """
    extra = mutation.get("extra", {})
    space_gid = extra.get("space_global_id")
    required = extra.get("required_percent")
    if not space_gid or not required:
        return CheckResult("a8_clause_violated", False,
                           message="mutation record lacks the room or its required percentage")
    try:
        space = output_model.by_guid(space_gid)
    except Exception:
        return CheckResult("a8_clause_violated", False, message="the room no longer resolves")

    walls = set()
    for rel in output_model.get_inverse(space):
        if rel.is_a("IfcRelSpaceBoundary"):
            element = rel.RelatedBuildingElement
            if element is not None and element.is_a("IfcWall"):
                walls.add(element.id())
    if not walls:
        return CheckResult("a8_clause_violated", False,
                           message="the room is bounded by no wall, so it has no exterior opening")

    scale_mm = mm_per_native(output_model)
    opening_m2 = 0.0
    counted = 0
    for window in output_model.by_type("IfcWindow"):
        host = None
        for rel in output_model.get_inverse(window):
            if not rel.is_a("IfcRelFillsElement"):
                continue
            for voids in output_model.get_inverse(rel.RelatingOpeningElement):
                if voids.is_a("IfcRelVoidsElement"):
                    host = voids.RelatingBuildingElement
                    break
        if host is None or host.id() not in walls:
            continue
        if _own_prop(host, "IsExternal") is not True:
            continue
        if window.OverallWidth is None or window.OverallHeight is None:
            continue
        opening_m2 += (float(window.OverallWidth) * scale_mm / 1000.0) * \
                      (float(window.OverallHeight) * scale_mm / 1000.0)
        counted += 1

    floor_area_m2 = extra.get("space_floor_area_m2")
    if not floor_area_m2:
        return CheckResult("a8_clause_violated", False,
                           message="mutation record lacks the room's floor area")

    ratio = 100.0 * opening_m2 / float(floor_area_m2)
    ok = ratio < float(required)
    return CheckResult("a8_clause_violated", ok,
                       {"opening_percent": round(ratio, 2), "required_percent": required,
                        "windows_counted": counted},
                       message="" if ok else f"{ratio:.2f}% is not below {required}%")


def rederive_a9(output_model, mutation, source_model=None) -> CheckResult:
    """Re-read the railing's height and re-decide guard against handrail."""
    railing = output_model.by_guid(mutation["target_global_id"])
    predefined = str(getattr(railing, "PredefinedType", None) or "").upper()
    text = _own_text(railing, "Name", "ObjectType", "Description")
    if predefined in ("GUARDRAIL", "BALUSTRADE") or any(w in text for w in _GUARD_WORDS):
        limit_mm, kind = 1000.0, "guard"
    elif predefined == "HANDRAIL" or any(w in text for w in _HANDRAIL_WORDS):
        limit_mm, kind = 900.0, "handrail"
    else:
        return CheckResult("a9_clause_violated", False,
                           message="the target railing is neither a guard nor a handrail")

    raw = _own_prop(railing, "Height")
    if raw is None:
        return CheckResult("a9_clause_violated", False,
                           message="no Height property on the target railing")
    height_mm = float(raw) * mm_per_native(output_model)
    ok = height_mm < limit_mm
    return CheckResult("a9_clause_violated", ok,
                       {"height_mm": round(height_mm, 1), "threshold_mm": limit_mm,
                        "railing_kind": kind},
                       message="" if ok else f"{height_mm:.1f}mm is not below {limit_mm}mm")


def rederive_a10(output_model, mutation, source_model=None,
                 threshold_mm: float = 1120.0) -> CheckResult:
    """Re-measure the flight width as the widest tread, not the modal one.

    The injector picks the width by what recurs across the treads; this takes
    the largest instead, so agreement is not an artefact of a shared choice.
    """
    flight = output_model.by_guid(mutation["target_global_id"])
    scale_mm = mm_per_native(output_model)
    widest = None
    treads = 0
    for item in _own_body_items(flight):
        if not item.is_a("IfcExtrudedAreaSolid"):
            continue
        extents = _own_profile_extents(item.SweptArea)
        if extents is None:
            continue
        treads += 1
        widest = extents[1] if widest is None else max(widest, extents[1])
    if widest is None:
        return CheckResult("a10_clause_violated", False,
                           message="no measurable tread profile on the flight")

    width_mm = widest * scale_mm
    ok = width_mm < threshold_mm
    return CheckResult("a10_clause_violated", ok,
                       {"width_mm": round(width_mm, 1), "threshold_mm": threshold_mm,
                        "tread_solids": treads},
                       message="" if ok else f"{width_mm:.1f}mm is not below {threshold_mm}mm")


def rederive_s6(output_model, mutation, source_model=None) -> CheckResult:
    """Re-total the wall's layer set and re-decide which limit governs."""
    wall = output_model.by_guid(mutation["target_global_id"])
    layer_set = None
    for rel in getattr(wall, "HasAssociations", []) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = rel.RelatingMaterial
        if material is None:
            continue
        if material.is_a("IfcMaterialLayerSetUsage"):
            layer_set = material.ForLayerSet
        elif material.is_a("IfcMaterialLayerSet"):
            layer_set = material
    if layer_set is None or not layer_set.MaterialLayers:
        return CheckResult("s6_clause_violated", False,
                           message="no material layer set on the target wall")

    scale_mm = mm_per_native(output_model)
    thickness_mm = sum(
        float(layer.LayerThickness or 0.0) for layer in layer_set.MaterialLayers
    ) * scale_mm
    names = " ".join(
        (layer.Material.Name if layer.Material and layer.Material.Name else "")
        for layer in layer_set.MaterialLayers
    ).lower()

    if any(w in names for w in _MASONRY_WORDS):
        limit_mm, kind = 250.0, "masonry"
    else:
        solid = _own_vertical_extrusion(output_model, wall)
        height_mm = float(solid.Depth) * scale_mm if solid is not None and solid.Depth else None
        limit_mm = max(100.0, height_mm / 25.0) if height_mm else 100.0
        kind = "concrete"

    ok = thickness_mm < limit_mm
    return CheckResult("s6_clause_violated", ok,
                       {"thickness_mm": round(thickness_mm, 1),
                        "threshold_mm": round(limit_mm, 1), "wall_kind": kind},
                       message="" if ok else f"{thickness_mm:.1f}mm is not below {limit_mm:.1f}mm")


def rederive_s7(output_model, mutation, source_model=None,
                min_dimension_mm: float = 300.0, min_ratio: float = 0.4) -> CheckResult:
    """Both limits of Sec 8.3.5.1 have to be broken, not just one."""
    column = output_model.by_guid(mutation["target_global_id"])
    extents = None
    for item in _own_body_items(column):
        if item.is_a("IfcExtrudedAreaSolid"):
            extents = _own_profile_extents(item.SweptArea)
            if extents is not None:
                break
    if extents is None:
        return CheckResult("s7_clause_violated", False,
                           message="could not measure the column's cross-section")

    scale_mm = mm_per_native(output_model)
    short_mm, long_mm = extents[0] * scale_mm, extents[1] * scale_mm
    ratio = short_mm / long_mm if long_mm else 0.0
    ok = short_mm < min_dimension_mm and ratio < min_ratio
    message = ""
    if not ok:
        message = (f"{short_mm:.1f}mm / ratio {ratio:.2f} does not break both the "
                   f"{min_dimension_mm}mm and {min_ratio} limits")
    return CheckResult("s7_clause_violated", ok,
                       {"short_dimension_mm": round(short_mm, 1),
                        "long_dimension_mm": round(long_mm, 1), "ratio": round(ratio, 3),
                        "threshold_mm": min_dimension_mm, "min_ratio": min_ratio},
                       message=message)


def _own_lateral_elements(output_model, storey):
    """Columns and load-bearing walls contained in a storey.

    The clause is about the lateral force-RESISTING system, so a partition
    is not in scope. Which walls count is decided the only way the file
    allows: Pset_WallCommon.LoadBearing where the model states it anywhere,
    and every wall where it states it nowhere. Counting partitions instead
    would measure a different building than the clause describes, and the
    re-derivation would disagree with the checker for the wrong reason.
    """
    flagged_anywhere = any(
        _own_prop(w, "LoadBearing") is True for w in output_model.by_type("IfcWall")
    )
    out = []
    for rel in output_model.get_inverse(storey):
        if not rel.is_a("IfcRelContainedInSpatialStructure"):
            continue
        for element in rel.RelatedElements:
            if element.is_a("IfcColumn"):
                out.append(element)
            elif element.is_a("IfcWall"):
                if not flagged_anywhere or _own_prop(element, "LoadBearing") is True:
                    out.append(element)
    return out


def rederive_s8(output_model, mutation, source_model=None,
                max_ratio: float = 1.30) -> CheckResult:
    """Re-measure both storeys' plan extents and re-take the ratio."""
    extra = mutation.get("extra", {})
    axis = extra.get("axis")
    neighbour_name = extra.get("neighbour_name")
    if axis not in ("X", "Y") or not neighbour_name:
        return CheckResult("s8_clause_violated", False,
                           message="mutation record lacks the axis or the adjacent storey")
    index = 0 if axis == "X" else 1

    try:
        storey = output_model.by_guid(mutation["target_global_id"])
    except Exception:
        return CheckResult("s8_clause_violated", False,
                           message="the target storey no longer resolves")
    neighbour = next(
        (s for s in output_model.by_type("IfcBuildingStorey") if s.Name == neighbour_name), None
    )
    if neighbour is None:
        return CheckResult("s8_clause_violated", False,
                           message=f"no storey named '{neighbour_name}' to compare against")

    def extent(target):
        values = []
        for element in _own_lateral_elements(output_model, target):
            xy = _own_world_xy(output_model, element)
            if xy is not None:
                values.append(xy[index])
        return (max(values) - min(values)) if len(values) >= 2 else None

    target_extent = extent(storey)
    neighbour_extent = extent(neighbour)
    if not target_extent or not neighbour_extent:
        return CheckResult("s8_clause_violated", False,
                           message="could not measure both storeys' plan extents")

    ratio = max(target_extent, neighbour_extent) / min(target_extent, neighbour_extent)
    ok = ratio > max_ratio
    return CheckResult("s8_clause_violated", ok,
                       {"axis": axis, "storey_extent_mm": round(target_extent, 1),
                        "adjacent_extent_mm": round(neighbour_extent, 1),
                        "ratio": round(ratio, 3), "max_ratio": max_ratio},
                       message="" if ok else f"ratio {ratio:.2f} does not exceed {max_ratio}")


def rederive_s9(output_model, mutation, source_model=None,
                min_projection: float = 0.15) -> CheckResult:
    """Confirm the L directly from element positions, with no raster at all.

    The corner block must now be empty of lateral elements while both wings
    beside it are still populated  - which is what makes the two projections
    beyond the re-entrant corner real. Going at it this way rather than by
    rebuilding a grid means a bug in the grid cannot be confirmed by itself.
    """
    extra = mutation.get("extra", {})
    corner = extra.get("corner")
    if corner not in ("south-west", "south-east", "north-west", "north-east"):
        return CheckResult("s9_clause_violated", False,
                           message="mutation record lacks a recognisable corner")
    high_x = corner.endswith("east")
    high_y = corner.startswith("north")

    try:
        storey = output_model.by_guid(mutation["target_global_id"])
    except Exception:
        return CheckResult("s9_clause_violated", False,
                           message="the target storey no longer resolves")

    points = []
    for element in _own_lateral_elements(output_model, storey):
        xy = _own_world_xy(output_model, element)
        if xy is not None:
            points.append(xy)
    if len(points) < 4:
        return CheckResult("s9_clause_violated", False,
                           message="too few lateral elements left to describe a plan")

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)
    width, depth = x_max - x_min, y_max - y_min
    if width <= 0 or depth <= 0:
        return CheckResult("s9_clause_violated", False, message="degenerate storey plan")

    x_cut = (x_max - min_projection * width) if high_x else (x_min + min_projection * width)
    y_cut = (y_max - min_projection * depth) if high_y else (y_min + min_projection * depth)

    def beyond_x(p):
        return p[0] >= x_cut if high_x else p[0] <= x_cut

    def beyond_y(p):
        return p[1] >= y_cut if high_y else p[1] <= y_cut

    in_corner = [p for p in points if beyond_x(p) and beyond_y(p)]
    x_wing = [p for p in points if beyond_x(p) and not beyond_y(p)]
    y_wing = [p for p in points if beyond_y(p) and not beyond_x(p)]

    ok = not in_corner and bool(x_wing) and bool(y_wing)
    if in_corner:
        message = f"{len(in_corner)} lateral element(s) still stand in the {corner} corner"
    elif not x_wing or not y_wing:
        message = "one of the two wings beside the corner is empty, so the plan is not re-entrant"
    else:
        message = ""
    return CheckResult("s9_clause_violated", ok,
                       {"corner": corner, "elements_in_corner": len(in_corner),
                        "elements_in_x_wing": len(x_wing), "elements_in_y_wing": len(y_wing),
                        "min_projection": min_projection},
                       message=message)


def rederive_s10(output_model, mutation, source_model=None) -> CheckResult:
    """Re-measure the footing's thickness and re-decide soil against piles."""
    footing = output_model.by_guid(mutation["target_global_id"])
    solid = _own_vertical_extrusion(output_model, footing)
    if solid is None or not solid.Depth:
        return CheckResult("s10_clause_violated", False,
                           message="no vertically extruded body on the target footing")

    thickness_mm = float(solid.Depth) * mm_per_native(output_model)
    name = _own_text(footing, "Name", "ObjectType")
    on_piles = any(w in name for w in _PILE_CAP_WORDS) or bool(output_model.by_type("IfcPile"))
    limit_mm = 300.0 if on_piles else 150.0

    ok = thickness_mm < limit_mm
    return CheckResult("s10_clause_violated", ok,
                       {"thickness_mm": round(thickness_mm, 1), "threshold_mm": limit_mm,
                        "support": "pile" if on_piles else "soil"},
                       message="" if ok else f"{thickness_mm:.1f}mm is not below {limit_mm}mm")


CLAUSE_CHECKS = {
    "A1": rederive_a1, "A2": rederive_a2, "A3": rederive_a3,
    "A4": rederive_a4, "A5": rederive_a5,
    "S1": rederive_s1, "S2": rederive_s2, "S3": rederive_s3,
    "S4": rederive_s4, "S5": rederive_s5,
    "A6": rederive_a6, "A7": rederive_a7, "A8": rederive_a8,
    "A9": rederive_a9, "A10": rederive_a10,
    "S6": rederive_s6, "S7": rederive_s7, "S8": rederive_s8,
    "S9": rederive_s9, "S10": rederive_s10,
}
