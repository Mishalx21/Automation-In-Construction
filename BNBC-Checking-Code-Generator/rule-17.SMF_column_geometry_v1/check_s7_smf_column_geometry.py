"""
Rule S7 — Cross-section geometry of special moment frame columns
(BNBC 2020 Part 6 Sec 8.3.5.1(a) and (b)).

Requirement, for a column of a special moment frame:
  (a) shortest cross-sectional dimension                >= 300 mm
  (b) shortest dimension / perpendicular dimension      >= 0.4

Conventions adopted:

* Which columns are in scope. Sec 8.3.5.1 governs "columns and other frame
  members serving to resist earthquake forces and having a factored axial
  force exceeding 0.1 Ag f'c". IFC carries no analysis results, so the
  factored axial force cannot be evaluated and every load-bearing CONCRETE
  column is treated as being in scope. That is the conservative reading:
  a lightly loaded concrete column below the clause's axial trigger is not
  actually governed by it, so a violation reported here should be read as
  "this column would not qualify as an SMF member", not as a settled
  non-compliance.

* Material. The clause is a reinforced-concrete detailing provision, so
  steel columns are out of scope — applying a 300 mm minimum to a 356 UC
  section would be meaningless. The material is taken from the column's
  material association, falling back to its name (this corpus labels
  sections "M_Concrete-Rectangular-Column", "kolom vierkant beton") when no
  material is attached. A column that is neither identifiably concrete nor
  identifiably steel is reported unknown.

* Section. Read from the column's own extruded profile: XDim/YDim for a
  rectangular profile, the profile's bounding rectangle otherwise. For a
  circular column the two dimensions are equal, so condition (b) is
  satisfied by construction and only the 300 mm diameter limit bites.

* Columns modelled as a mesh with no profile are reported unknown rather
  than measured from a bounding box, because a bounding box around a
  rotated or tapered member misstates the section.

Usage:
    python check_s7_smf_column_geometry.py <path-to-ifc>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ifcopenshell

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ifc_helpers.helpers import (  # noqa: E402
    element_label, element_storey, length_unit_to_mm, property_sets,
)

RULE_ID = "S7"
RULE_REF = "BNBC 2020 Part 6 Sec 8.3.5.1(a), (b)"
MIN_SHORT_DIMENSION_MM = 300.0
MIN_DIMENSION_RATIO = 0.4
COND_DIMENSION = "smf_column_min_dimension"
COND_RATIO = "smf_column_dimension_ratio"

# Outside this band the section is a dowel, a baseplate detail or a whole
# column stack swept into one solid, not a column cross-section.
MIN_PLAUSIBLE_DIMENSION_MM = 80.0
MAX_PLAUSIBLE_DIMENSION_MM = 4000.0

_CONCRETE_KEYWORDS = ("concrete", "beton", "rc ", "reinforced")
_STEEL_KEYWORDS = (
    "steel", "staal", "metal", "wide flange", "universal column", "uc-",
    "hss", "shs", "chs", "s235", "s275", "s355",
)


def _material_names(element) -> list[str]:
    names: list[str] = []
    for rel in getattr(element, "HasAssociations", None) or []:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = getattr(rel, "RelatingMaterial", None)
        if material is None:
            continue
        if material.is_a("IfcMaterial"):
            names.append(str(getattr(material, "Name", "") or ""))
        elif material.is_a("IfcMaterialList"):
            names.extend(str(getattr(m, "Name", "") or "") for m in material.Materials)
        elif material.is_a("IfcMaterialLayerSetUsage") or material.is_a("IfcMaterialLayerSet"):
            layer_set = material if material.is_a("IfcMaterialLayerSet") else material.ForLayerSet
            for layer in getattr(layer_set, "MaterialLayers", None) or []:
                names.append(str(getattr(getattr(layer, "Material", None), "Name", "") or ""))
        elif material.is_a("IfcMaterialProfileSetUsage") or material.is_a("IfcMaterialProfileSet"):
            profile_set = material if material.is_a("IfcMaterialProfileSet") else material.ForProfileSet
            for profile in getattr(profile_set, "MaterialProfiles", None) or []:
                names.append(str(getattr(getattr(profile, "Material", None), "Name", "") or ""))
    return [n for n in names if n]


def _element_type_name(element) -> str:
    """The column's own name plus its type's, for the material fallback."""
    parts = [str(getattr(element, attr, None) or "") for attr in ("Name", "ObjectType")]
    for rel in getattr(element, "IsTypedBy", None) or getattr(element, "IsDefinedBy", None) or []:
        if rel.is_a("IfcRelDefinesByType"):
            parts.append(str(getattr(getattr(rel, "RelatingType", None), "Name", "") or ""))
    return " ".join(parts)


def _classify_material(element) -> str | None:
    """'concrete' | 'steel' | None (undecidable)."""
    text = " ".join(_material_names(element)).lower()
    if not text.strip():
        # No material association: the section name is the only evidence.
        text = _element_type_name(element).lower()
    if not text.strip():
        return None
    if any(k in text for k in _CONCRETE_KEYWORDS):
        return "concrete"
    if any(k in text for k in _STEEL_KEYWORDS):
        return "steel"
    return None


def _items_of(element, identifier: str):
    representation = getattr(element, "Representation", None)
    if representation is None:
        return []
    out = []
    for rep in getattr(representation, "Representations", None) or []:
        if getattr(rep, "RepresentationIdentifier", None) != identifier:
            continue
        for item in getattr(rep, "Items", None) or []:
            if item.is_a("IfcMappedItem"):
                mapped = item.MappingSource.MappedRepresentation
                out.extend(getattr(mapped, "Items", None) or [])
            else:
                out.append(item)
    return out


def _profile_dimensions(profile):
    """(short, long) in-plane extent of a profile, in native units."""
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
        points = [tuple(p.Coordinates[:2]) for p in curve.Points]
    elif curve is not None and curve.is_a("IfcCompositeCurve"):
        for segment in getattr(curve, "Segments", None) or []:
            parent = getattr(segment, "ParentCurve", None)
            if parent is not None and parent.is_a("IfcPolyline"):
                points.extend(tuple(p.Coordinates[:2]) for p in parent.Points)
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    dims = (max(xs) - min(xs), max(ys) - min(ys))
    return min(dims), max(dims)


def _section_mm(column, unit_mm: float):
    """(short mm, long mm) cross-section of the column, or None."""
    for item in _items_of(column, "Body"):
        if item.is_a("IfcExtrudedAreaSolid"):
            dims = _profile_dimensions(getattr(item, "SweptArea", None))
            if dims is not None:
                return round(dims[0] * unit_mm, 1), round(dims[1] * unit_mm, 1)
    return None


def _is_load_bearing(column) -> bool | None:
    value = property_sets(column).get("Pset_ColumnCommon", {}).get("LoadBearing")
    return None if value is None else bool(value)


def check_rule(model: ifcopenshell.file) -> dict:
    columns = model.by_type("IfcColumn")

    if not columns:
        return {
            "verdict": "not_applicable", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": {},
            "summary": "No IfcColumn elements found in the model.",
            "checks": [],
        }

    unit_mm = length_unit_to_mm(model)

    violations = []
    checks = []
    checked = 0
    steel_columns = 0
    non_bearing = 0
    unknown_material = 0
    no_section = 0
    implausible = 0

    for column in columns:
        # An explicit LoadBearing = False is the model saying this is not a
        # frame member; a missing flag is not, so it stays in scope.
        if _is_load_bearing(column) is False:
            non_bearing += 1
            continue

        kind = _classify_material(column)
        if kind == "steel":
            steel_columns += 1
            continue
        if kind is None:
            unknown_material += 1
            continue

        section = _section_mm(column, unit_mm)
        if section is None:
            no_section += 1
            continue
        short_mm, long_mm = section
        if not (MIN_PLAUSIBLE_DIMENSION_MM <= short_mm <= MAX_PLAUSIBLE_DIMENSION_MM):
            implausible += 1
            continue

        checked += 1
        label = element_label(column)
        storey = element_storey(column)
        ratio = short_mm / long_mm if long_mm else 0.0

        dimension_measured = (
            f"short_dimension={short_mm:.0f} mm, "
            f"required={MIN_SHORT_DIMENSION_MM:.0f} mm, "
            f"section={short_mm:.0f}x{long_mm:.0f} mm"
        )
        dimension_threshold = f">= {MIN_SHORT_DIMENSION_MM:.0f} mm"
        dimension_is_violation = short_mm < MIN_SHORT_DIMENSION_MM
        checks.append(
            {
                "element": label,
                "storey": storey,
                "criterion": "Minimum short dimension",
                "measured": dimension_measured,
                "threshold": dimension_threshold,
                "result": "fail" if dimension_is_violation else "pass",
            }
        )
        if dimension_is_violation:
            violations.append({
                "condition": COND_DIMENSION,
                "description": (
                    "Shortest cross-sectional dimension of a special moment frame "
                    "column is below the minimum."
                ),
                "rule_ref": RULE_REF,
                "threshold": dimension_threshold,
                "locations": [{
                    "element": label, "storey": storey,
                    "measured": dimension_measured,
                }],
            })

        ratio_measured = (
            f"ratio={ratio:.2f}, required={MIN_DIMENSION_RATIO:.1f}, "
            f"section={short_mm:.0f}x{long_mm:.0f} mm"
        )
        ratio_threshold = f">= {MIN_DIMENSION_RATIO:.1f}"
        ratio_is_violation = ratio < MIN_DIMENSION_RATIO
        checks.append(
            {
                "element": label,
                "storey": storey,
                "criterion": "Dimension ratio",
                "measured": ratio_measured,
                "threshold": ratio_threshold,
                "result": "fail" if ratio_is_violation else "pass",
            }
        )
        if ratio_is_violation:
            violations.append({
                "condition": COND_RATIO,
                "description": (
                    "Ratio of the shortest cross-sectional dimension to the "
                    "perpendicular dimension is below the minimum."
                ),
                "rule_ref": RULE_REF,
                "threshold": ratio_threshold,
                "locations": [{
                    "element": label, "storey": storey,
                    "measured": ratio_measured,
                }],
            })

    unknown_reasons = []
    if unknown_material:
        unknown_reasons.append({
            "condition": COND_DIMENSION,
            "missing": "material or section name identifying the column as concrete or steel",
            "affected_elements": unknown_material,
        })
    if no_section:
        unknown_reasons.append({
            "condition": COND_DIMENSION,
            "missing": "extruded profile giving the column's cross-section",
            "affected_elements": no_section,
        })

    skip_reasons = {
        k: v for k, v in (
            ("steel_column_out_of_scope", steel_columns),
            ("not_load_bearing", non_bearing),
            ("unidentified_material", unknown_material),
            ("missing_section", no_section),
            ("implausible_section", implausible),
        ) if v
    }
    checked_summary = {
        condition: {
            "elements_checked": checked,
            "elements_skipped": len(columns) - checked,
            "skip_reasons": skip_reasons,
        }
        for condition in (COND_DIMENSION, COND_RATIO)
    }

    if violations:
        verdict = "fail"
        summary = (
            f"{len(violations)} special moment frame column condition(s) fail across "
            f"{checked} checked concrete column(s)."
        )
    elif checked == 0:
        if steel_columns and not (unknown_material or no_section):
            verdict = "not_applicable"
            summary = (
                f"All {steel_columns} column(s) are steel; Sec 8.3.5.1 is a reinforced "
                f"concrete provision."
            )
        else:
            verdict = "unknown"
            summary = (
                f"None of the {len(columns)} column(s) have both an identifiable concrete "
                f"material and a measurable section; compliance cannot be determined."
            )
    else:
        verdict = "pass"
        summary = (
            f"All {checked} concrete column(s) meet the {MIN_SHORT_DIMENSION_MM:.0f} mm minimum "
            f"dimension and the {MIN_DIMENSION_RATIO:.1f} aspect ratio."
        )

    return {
        "verdict": verdict,
        "violations": violations,
        "violation_count": len(violations),
        "unknown_reasons": unknown_reasons,
        "checked_summary": checked_summary,
        "summary": summary,
        "checks": checks,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(__file__).name} <path-to-ifc>")
        return 2

    model = ifcopenshell.open(sys.argv[1])
    result = check_rule(model)

    print(json.dumps(result, indent=2))
    print(f"\nVerdict: {result['verdict'].upper()}")
    for v in result["violations"]:
        for loc in v["locations"]:
            print(f"  - [{v['condition']}] {loc['element']} @ {loc['storey']}: {loc['measured']}")

    return 1 if result["verdict"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
