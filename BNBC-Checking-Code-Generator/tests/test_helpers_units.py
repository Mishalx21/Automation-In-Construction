"""Tests for length_unit_to_mm: scale detection and default-on-missing."""

from __future__ import annotations

import logging

import pytest

from ifc_helpers import length_unit_to_mm
from bnbc.fixtures import synthetic as ifc_builders


class TestScaleDetection:

    def test_millimetre_model_scale_is_1(self, mm_model):
        model, _ = mm_model
        assert length_unit_to_mm(model) == pytest.approx(1.0)

    def test_metre_model_scale_is_1000(self, m_model):
        model, _ = m_model
        assert length_unit_to_mm(model) == pytest.approx(1000.0)

    def test_centimetre_model_scale_is_10(self):
        model, _ = ifc_builders.make_model(units="cm")
        assert length_unit_to_mm(model) == pytest.approx(10.0)

    def test_inch_conversion_based_unit(self):
        model, _ = ifc_builders.make_model(units="inch")
        assert length_unit_to_mm(model) == pytest.approx(25.4)

    def test_detection_emits_no_warning_when_units_present(
        self, mm_model, caplog
    ):
        model, _ = mm_model
        with caplog.at_level(logging.WARNING, logger="ifc_helpers.helpers"):
            length_unit_to_mm(model)
        assert caplog.records == []


class TestDefaultOnMissing:
    """When units are missing the helper must assume MILLIMETRES (1.0)
    and warn — never silently assume metres (1000.0), which produced
    catastrophic 1000x errors."""

    def test_project_without_units_defaults_to_mm(self, unitless_model):
        model, _ = unitless_model
        assert length_unit_to_mm(model) == pytest.approx(1.0)

    def test_project_without_units_logs_warning(self, unitless_model, caplog):
        model, _ = unitless_model
        with caplog.at_level(logging.WARNING, logger="ifc_helpers.helpers"):
            length_unit_to_mm(model)
        assert any("assuming" in r.message.lower() for r in caplog.records)

    def test_model_without_project_defaults_to_mm(self, projectless_model):
        model, _ = projectless_model
        assert length_unit_to_mm(model) == pytest.approx(1.0)

    def test_model_without_project_logs_warning(
        self, projectless_model, caplog
    ):
        model, _ = projectless_model
        with caplog.at_level(logging.WARNING, logger="ifc_helpers.helpers"):
            length_unit_to_mm(model)
        assert any(
            "no ifcproject" in r.message.lower() for r in caplog.records
        )

    def test_units_present_but_no_length_unit_defaults_to_mm(self, caplog):
        # Angle unit only — the LENGTHUNIT scan falls through.
        model, _ = ifc_builders.make_model(units=None, angle_unit="radian")
        with caplog.at_level(logging.WARNING, logger="ifc_helpers.helpers"):
            assert length_unit_to_mm(model) == pytest.approx(1.0)
        assert any("lengthunit" in r.message.lower() for r in caplog.records)

    def test_missing_default_is_never_metres(self, unitless_model):
        """Regression: the old silent default was 1000.0 (metres)."""
        model, _ = unitless_model
        assert length_unit_to_mm(model) != pytest.approx(1000.0)
