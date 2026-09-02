"""Operator registry. Importing this package registers every operator."""

from bnbc.fixtures.operators.base import OPERATORS, Operator, OperatorResult, register

# Import for registration side effects (each module @registers its operators).
from bnbc.fixtures.operators import geometry, insert, property_ops, structure  # noqa: E402,F401

__all__ = ["OPERATORS", "Operator", "OperatorResult", "register"]
