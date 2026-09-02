"""Shared pytest fixtures for the ifc_helpers test suite."""

from __future__ import annotations

import pathlib
import sys

import pytest

# Make the repo root importable regardless of the pytest invocation dir.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bnbc.fixtures import synthetic as ifc_builders  # noqa: E402

REAL_IFC_PATH = REPO_ROOT / "ifc-files" / "2501_WD-4_CEVTA_DORM T-1_STR.ifc"


@pytest.fixture
def mm_model():
    """(model, context) authored in millimetres, plane angles in radians."""
    return ifc_builders.make_model(units="mm", angle_unit="radian")


@pytest.fixture
def m_model():
    """(model, context) authored in metres, plane angles in radians."""
    return ifc_builders.make_model(units="m", angle_unit="radian")


@pytest.fixture
def unitless_model():
    """(model, context) whose IfcProject has UnitsInContext=None."""
    return ifc_builders.make_model(with_units=False)


@pytest.fixture
def projectless_model():
    """(model, context) with no IfcProject at all."""
    return ifc_builders.make_model(with_project=False)
