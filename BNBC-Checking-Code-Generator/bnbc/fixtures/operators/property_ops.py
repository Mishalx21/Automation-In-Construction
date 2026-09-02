"""Property perturbation operators (attributes, psets, materials).

These manufacture the *unknown*-verdict fixtures (data stripped) and the
property-violation fixtures (diameter/grade changes) of a fixture plan.
"""

from __future__ import annotations

from typing import Any, Optional

import ifcopenshell

from bnbc.fixtures import measure
from bnbc.fixtures.errors import TargetNotFoundError
from bnbc.fixtures.operators.base import Operator, OperatorResult, register


def _wrapped_value(model, value):
    if isinstance(value, bool):
        return model.create_entity("IfcBoolean", bool(value))
    if isinstance(value, (int, float)):
        return model.create_entity("IfcReal", float(value))
    return model.create_entity("IfcLabel", str(value))


@register
class SetPsetProperty(Operator):
    """Set (create or update) one single-value property in a named pset.

    The generic knob for property-dependent thresholds — e.g. pin concrete
    'CompressiveStrength' or steel 'YieldStrength' to the exact value a
    condition's formula needs. Numbers are written as IfcReal, booleans as
    IfcBoolean, everything else as IfcLabel.
    """

    name = "set_pset_property"

    def _apply(self, model, target_selector, *, pset_name: str, property_name: str,
               value: Any, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector, "IfcReinforcingBar")
        guids, expected = [], []
        for el in targets:
            pset = None
            for rel in getattr(el, "IsDefinedBy", None) or []:
                if not rel.is_a("IfcRelDefinesByProperties"):
                    continue
                pd = getattr(rel, "RelatingPropertyDefinition", None)
                if pd is not None and pd.is_a("IfcPropertySet") and \
                        str(getattr(pd, "Name", "") or "") == pset_name:
                    pset = pd
                    break
            prop = model.create_entity(
                "IfcPropertySingleValue", Name=property_name,
                NominalValue=_wrapped_value(model, value),
            )
            if pset is None:
                pset = model.create_entity(
                    "IfcPropertySet", GlobalId=ifcopenshell.guid.new(),
                    Name=pset_name, HasProperties=[prop],
                )
                model.create_entity(
                    "IfcRelDefinesByProperties", GlobalId=ifcopenshell.guid.new(),
                    RelatedObjects=[el], RelatingPropertyDefinition=pset,
                )
            else:
                kept = [
                    p for p in (pset.HasProperties or [])
                    if not (p.is_a("IfcPropertySingleValue")
                            and str(getattr(p, "Name", "")) == property_name)
                ]
                pset.HasProperties = kept + [prop]
            guids.append(el.GlobalId)
            expected.append({
                "check": "pset_value", "guid": el.GlobalId,
                "pset_name": pset_name, "name": property_name, "value": value,
            })
        return OperatorResult(
            model=model, operator=self.name,
            params={"pset_name": pset_name, "property_name": property_name, "value": value},
            affected_guids=guids,
            claim=f"{pset_name}.{property_name} set to {value!r} on {len(guids)} element(s)",
            expected=expected,
        )


@register
class SetNominalDiameter(Operator):
    """Set ``NominalDiameter`` of reinforcing elements to ``diameter_mm``.

    The value is written in model length units (converted from mm), producing
    diameter-threshold violations with a known magnitude.
    """

    name = "set_nominal_diameter"

    def _apply(self, model, target_selector, *, diameter_mm: float, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector, "IfcReinforcingBar")
        scale = measure.unit_scale_mm(model)
        guids, expected = [], []
        for el in targets:
            el.NominalDiameter = float(diameter_mm) / scale
            guids.append(el.GlobalId)
            expected.append({
                "check": "diameter", "guid": el.GlobalId,
                "value_mm": float(diameter_mm), "tol_mm": 0.01,
            })
        return OperatorResult(
            model=model, operator=self.name,
            params={"diameter_mm": float(diameter_mm)},
            affected_guids=guids,
            claim=f"NominalDiameter of {len(guids)} element(s) set to {diameter_mm:.1f} mm",
            expected=expected,
        )


@register
class StripAttribute(Operator):
    """Null out one direct attribute (e.g. ``NominalDiameter``) — expect ``unknown``."""

    name = "strip_attribute"

    def _apply(self, model, target_selector, *, attribute: str, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector, "IfcReinforcingBar")
        guids, expected = [], []
        from bnbc.fixtures.errors import PlanningError
        for el in targets:
            if not hasattr(el, attribute):
                raise TargetNotFoundError(f"{el.is_a()} has no attribute {attribute!r}")
            try:
                setattr(el, attribute, None)
            except (TypeError, ValueError, RuntimeError, Exception) as exc:
                raise PlanningError(
                    f"attribute {attribute!r} is not optional for {el.is_a()} and cannot be stripped. "
                    f"Error: {exc}. Try using 'strip_pset' or another operator instead."
                )
            guids.append(el.GlobalId)
            expected.append({
                "check": "attribute", "guid": el.GlobalId, "name": attribute, "value": None,
            })
        return OperatorResult(
            model=model, operator=self.name,
            params={"attribute": attribute},
            affected_guids=guids,
            claim=f"attribute {attribute} stripped from {len(guids)} element(s)",
            expected=expected,
        )


@register
class StripPset(Operator):
    """Detach property sets (by exact name, or all with ``pset_name=None``) — expect ``unknown``."""

    name = "strip_pset"

    def _apply(self, model, target_selector, *, pset_name: Optional[str] = None,
               **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector, "IfcReinforcingBar")
        guids, expected = [], []
        stripped = 0
        for el in targets:
            for rel in list(getattr(el, "IsDefinedBy", None) or []):
                if not rel.is_a("IfcRelDefinesByProperties"):
                    continue
                pd = getattr(rel, "RelatingPropertyDefinition", None)
                if pd is None or not pd.is_a("IfcPropertySet"):
                    continue
                if pset_name is not None and str(getattr(pd, "Name", "") or "") != pset_name:
                    continue
                remaining = [o for o in rel.RelatedObjects if o != el]
                if remaining:
                    rel.RelatedObjects = remaining
                else:
                    model.remove(rel)
                stripped += 1
            guids.append(el.GlobalId)
            expected.append({
                "check": "pset_absent", "guid": el.GlobalId, "name": pset_name or "*",
            })
        return OperatorResult(
            model=model, operator=self.name,
            params={"pset_name": pset_name},
            affected_guids=guids,
            claim=f"stripped {stripped} pset link(s) ({pset_name or 'all psets'}) "
                  f"from {len(guids)} element(s)",
            expected=expected,
        )


@register
class RenameMaterial(Operator):
    """Rename the materials associated with the target elements (grade change).

    Every ``IfcMaterial`` reachable through the targets' material associations
    (direct, list, layer set, profile set) whose name matches ``old_name``
    (or all of them when ``old_name`` is None) gets ``new_name``.
    """

    name = "rename_material"

    def _apply(self, model, target_selector, *, new_name: str,
               old_name: Optional[str] = None, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector, "IfcReinforcingBar")
        guids, expected = [], []
        renamed = 0
        for el in targets:
            for rel in getattr(el, "HasAssociations", None) or []:
                if not rel.is_a("IfcRelAssociatesMaterial"):
                    continue
                for mat in _reachable_materials(getattr(rel, "RelatingMaterial", None)):
                    if old_name is not None and str(getattr(mat, "Name", "") or "") != old_name:
                        continue
                    mat.Name = new_name
                    renamed += 1
            guids.append(el.GlobalId)
            expected.append({"check": "material_name", "guid": el.GlobalId, "value": new_name})
        if renamed == 0:
            raise TargetNotFoundError(
                f"no material named {old_name!r} found on the target element(s)"
            )
        return OperatorResult(
            model=model, operator=self.name,
            params={"new_name": new_name, "old_name": old_name},
            affected_guids=guids,
            claim=f"renamed {renamed} material(s) to {new_name!r} on {len(guids)} element(s)",
            expected=expected,
        )


def _reachable_materials(material) -> list:
    """All IfcMaterial entities reachable from an IfcMaterialSelect value."""
    if material is None:
        return []
    if material.is_a("IfcMaterial"):
        return [material]
    out = []
    if material.is_a("IfcMaterialList"):
        for m in getattr(material, "Materials", None) or []:
            out.extend(_reachable_materials(m))
    elif material.is_a("IfcMaterialLayerSetUsage"):
        out.extend(_reachable_materials(getattr(material, "ForLayerSet", None)))
    elif material.is_a("IfcMaterialLayerSet"):
        for layer in getattr(material, "MaterialLayers", None) or []:
            out.extend(_reachable_materials(getattr(layer, "Material", None)))
    elif material.is_a("IfcMaterialProfileSetUsage"):
        out.extend(_reachable_materials(getattr(material, "ForProfileSet", None)))
    elif material.is_a("IfcMaterialProfileSet"):
        for profile in getattr(material, "MaterialProfiles", None) or []:
            out.extend(_reachable_materials(getattr(profile, "Material", None)))
    return out
