"""Structural operators: element deletion (spacing/count violations and
``not_applicable`` fixtures).

Deletion detaches the element from the objectified relationships that point
at it (containment, property, material, aggregation) — removing empty
relationships outright — then removes the entity. Orphaned geometry entities
are left behind; checkers query ``by_type``/relations, never reachability,
so the file stays valid for fixture purposes.
"""

from __future__ import annotations


from bnbc.fixtures.errors import TargetNotFoundError
from bnbc.fixtures.operators.base import Operator, OperatorResult, register

#: (inverse attr on the element, forward list attr on the relationship)
_DETACH_RELS = (
    ("ContainedInStructure", "RelatedElements"),
    ("IsDefinedBy", "RelatedObjects"),
    ("HasAssociations", "RelatedObjects"),
    ("Decomposes", "RelatedObjects"),
)


def _detach_and_remove(model, element) -> None:
    for inverse_name, forward_name in _DETACH_RELS:
        for rel in list(getattr(element, inverse_name, None) or []):
            remaining = [o for o in getattr(rel, forward_name) if o != element]
            if remaining:
                setattr(rel, forward_name, remaining)
            else:
                model.remove(rel)
    model.remove(element)


@register
class DeleteElements(Operator):
    """Delete specific elements (spacing/count violations)."""

    name = "delete_elements"

    def _apply(self, model, target_selector, **_ignored) -> OperatorResult:
        targets = self.resolve_targets(model, target_selector)
        guids = [el.GlobalId for el in targets]
        classes = {el.GlobalId: el.is_a() for el in targets}
        for el in targets:
            _detach_and_remove(model, el)
        return OperatorResult(
            model=model, operator=self.name, params={},
            affected_guids=guids,
            claim=f"deleted {len(guids)} element(s)",
            expected=[
                {"check": "guid_absent", "guid": g, "ifc_class": classes[g]} for g in guids
            ],
        )


@register
class DeleteElementsOfType(Operator):
    """Delete every element of one IFC class — the ``not_applicable`` fixture."""

    name = "delete_elements_of_type"

    def _apply(self, model, target_selector, *, ifc_class: str, **_ignored) -> OperatorResult:
        targets = list(model.by_type(ifc_class))
        if not targets:
            raise TargetNotFoundError(f"model has no {ifc_class} elements to delete")
        guids = [el.GlobalId for el in targets]
        for el in targets:
            _detach_and_remove(model, el)
        return OperatorResult(
            model=model, operator=self.name, params={"ifc_class": ifc_class},
            affected_guids=guids,
            claim=f"deleted all {len(guids)} {ifc_class} element(s)",
            expected=[{"check": "type_count", "ifc_class": ifc_class, "value": 0}],
        )
