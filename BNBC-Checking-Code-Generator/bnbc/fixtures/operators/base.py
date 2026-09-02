"""Operator ABC and result type for deterministic IFC perturbations.

Every operator is PURE with respect to its input: ``apply(model, ...)``
serialises the input model and works on a fresh in-memory copy, so the
caller's model object is never mutated.

An :class:`OperatorResult` carries, besides the perturbed model, a list of
machine-checkable ``expected`` records. These are the input to
``bnbc.fixtures.selfverify`` which — after the model is written to disk —
re-opens the file and independently re-measures every record with
``bnbc.fixtures.measure`` (no ifc_helpers involvement, by design).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

import ifcopenshell

from bnbc.fixtures.errors import TargetNotFoundError

TargetSelector = Union[None, str, list, tuple, Callable[[ifcopenshell.file], list]]


@dataclass
class OperatorResult:
    """Outcome of one operator application (in-memory; nothing written yet)."""

    model: ifcopenshell.file
    operator: str
    params: dict[str, Any]
    affected_guids: list[str]
    claim: str
    expected: list[dict[str, Any]] = field(default_factory=list)
    """Machine-checkable expectations, consumed by ``selfverify``. Each record
    is a dict with a ``check`` key (e.g. ``terminal_tail``, ``arc_angle``,
    ``attribute``, ``pset_absent``, ``type_count``, ``guid_present``,
    ``guid_absent``, ``placement_origin``, ``hook``, ``material_name``,
    ``storey``, ``diameter``) plus check-specific fields."""


def copy_model(model: ifcopenshell.file) -> ifcopenshell.file:
    """Deep copy an in-memory IFC model via serialisation round-trip."""
    return ifcopenshell.file.from_string(model.to_string())


class Operator(ABC):
    """A deterministic, pure, self-describing perturbation operator."""

    #: Registry key; also recorded in FixtureSpec.operator.
    name: str = ""

    #: Declarative numeric parameter domains: ``{param: (min, max)}`` with
    #: ``None`` for an open bound. The PLANNER validates sketch params against
    #: these, so a degenerate param is a plan-time defect (repairable, with a
    #: sketch index) instead of a build-time crash.
    PARAM_DOMAINS: dict[str, tuple] = {}
    #: Optional guidance appended to a domain-violation defect message.
    DOMAIN_NOTE: str = ""

    def apply(self, model: ifcopenshell.file, target_selector: TargetSelector = None,
              **params: Any) -> OperatorResult:
        """Apply to a COPY of ``model``; the input instance is not mutated."""
        working = copy_model(model)
        return self._apply(working, target_selector, **params)

    @abstractmethod
    def _apply(self, model: ifcopenshell.file, target_selector: TargetSelector,
               **params: Any) -> OperatorResult:
        """Mutate ``model`` (already a private copy) and describe the change."""

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def resolve_targets(model: ifcopenshell.file, target_selector: TargetSelector,
                        default_type: Optional[str] = None) -> list:
        """Resolve a target selector to entity instances.

        Accepts a GlobalId string, a list of GlobalIds, a callable
        ``model -> [elements]``, or ``None`` (all elements of
        ``default_type``).
        """
        if callable(target_selector):
            targets = list(target_selector(model))
        elif isinstance(target_selector, str):
            targets = [model.by_guid(target_selector)]
        elif isinstance(target_selector, (list, tuple)):
            targets = [model.by_guid(g) if isinstance(g, str) else g for g in target_selector]
        elif target_selector is None and default_type:
            targets = list(model.by_type(default_type))
        else:
            targets = []
        if not targets:
            raise TargetNotFoundError(
                f"no targets resolved (selector={target_selector!r}, default_type={default_type!r})"
            )
        return targets


# Registry: operator name -> class. Populated by the operator modules.
OPERATORS: dict[str, type[Operator]] = {}


def register(cls: type[Operator]) -> type[Operator]:
    OPERATORS[cls.name] = cls
    return cls
