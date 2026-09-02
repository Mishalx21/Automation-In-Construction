"""Geometry tests for get_bar_bend_info on synthetic bars.

Each bar is constructed with exactly known bend angle, bend radius,
lead-in and tail lengths (see bnbc/fixtures/synthetic.py), so every value
returned by the helper can be asserted against ground truth:

* bend angle within 1 degree of the constructed truth,
* length_mm / radius_mm within 0.5 mm,
* on BOTH directrix encodings (IfcIndexedPolyCurve with IfcArcIndex,
  and IfcCompositeCurve with IfcTrimmedCurve-on-IfcCircle).
"""

from __future__ import annotations

import math

import pytest

from ifc_helpers import get_bar_bend_info
from bnbc.fixtures import synthetic as ifc_builders

ANGLE_TOL_DEG = 1.0
MM_TOL = 0.5


def arcs_of(segments):
    return [s for s in segments if s["type"] == "arc"]


def straights_of(segments):
    return [s for s in segments if s["type"] == "straight"]


# ─────────────────────────────────────────────────────────────────────
# IfcIndexedPolyCurve (IfcLineIndex / IfcArcIndex) hooks
# ─────────────────────────────────────────────────────────────────────
class TestIndexedPolyCurveHooks:

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_angle_within_1_degree(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx, bend_angle_deg=bend_angle
        )
        segments = get_bar_bend_info(bar)
        arcs = arcs_of(segments)
        assert len(arcs) == 1, f"expected exactly one arc, got {segments}"
        assert arcs[0]["angle_deg"] == pytest.approx(
            truth["bend_angle_deg"], abs=ANGLE_TOL_DEG
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_radius_mm_within_half_mm(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx, bend_angle_deg=bend_angle, bend_radius=48.0
        )
        arcs = arcs_of(get_bar_bend_info(bar))
        assert arcs[0]["radius_mm"] == pytest.approx(
            truth["bend_radius"], abs=MM_TOL
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_arc_length_mm_within_half_mm(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx, bend_angle_deg=bend_angle
        )
        arcs = arcs_of(get_bar_bend_info(bar))
        # arc_length = radius * radians(true bend angle)
        assert arcs[0]["length_mm"] == pytest.approx(
            truth["arc_length"], abs=MM_TOL
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_segment_structure_and_straight_lengths(
        self, mm_model, bend_angle
    ):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx, bend_angle_deg=bend_angle,
            lead_in=250.0, tail=120.0,
        )
        segments = get_bar_bend_info(bar)
        assert [s["type"] for s in segments] == ["straight", "arc", "straight"]
        assert segments[0]["length_mm"] == pytest.approx(
            truth["lead_in"], abs=MM_TOL
        )
        assert segments[2]["length_mm"] == pytest.approx(
            truth["tail"], abs=MM_TOL
        )

    @pytest.mark.parametrize(
        "bend_angle,bend_radius,tail",
        [
            (90.0, 30.0, 60.0),
            (90.0, 64.0, 192.0),
            (135.0, 48.0, 96.0),
            (180.0, 40.0, 65.0),
        ],
    )
    def test_hook_parameter_sweep(self, mm_model, bend_angle, bend_radius, tail):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx,
            bend_angle_deg=bend_angle, bend_radius=bend_radius, tail=tail,
        )
        segments = get_bar_bend_info(bar)
        arcs = arcs_of(segments)
        assert len(arcs) == 1
        assert arcs[0]["angle_deg"] == pytest.approx(bend_angle, abs=ANGLE_TOL_DEG)
        assert arcs[0]["radius_mm"] == pytest.approx(bend_radius, abs=MM_TOL)
        assert segments[-1]["length_mm"] == pytest.approx(tail, abs=MM_TOL)

    def test_straight_segments_have_zero_angle_and_radius(self, mm_model):
        model, ctx = mm_model
        bar, _ = ifc_builders.make_hook_bar_indexed(model, ctx)
        for seg in straights_of(get_bar_bend_info(bar)):
            assert seg["angle_deg"] == 0.0
            assert seg["radius_mm"] == 0.0


# ─────────────────────────────────────────────────────────────────────
# IfcCompositeCurve (IfcTrimmedCurve on IfcCircle) hooks
# ─────────────────────────────────────────────────────────────────────
class TestCompositeCurveHooks:

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_angle_within_1_degree(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=bend_angle
        )
        segments = get_bar_bend_info(bar)
        arcs = arcs_of(segments)
        assert len(arcs) == 1, f"expected exactly one arc, got {segments}"
        assert arcs[0]["angle_deg"] == pytest.approx(
            truth["bend_angle_deg"], abs=ANGLE_TOL_DEG
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_radius_and_arc_length_mm(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=bend_angle, bend_radius=48.0
        )
        arcs = arcs_of(get_bar_bend_info(bar))
        assert arcs[0]["radius_mm"] == pytest.approx(
            truth["bend_radius"], abs=MM_TOL
        )
        assert arcs[0]["length_mm"] == pytest.approx(
            truth["arc_length"], abs=MM_TOL
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_hook_straight_legs(self, mm_model, bend_angle):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=bend_angle,
            lead_in=220.0, tail=110.0,
        )
        segments = get_bar_bend_info(bar)
        assert [s["type"] for s in segments] == ["straight", "arc", "straight"]
        assert segments[0]["length_mm"] == pytest.approx(
            truth["lead_in"], abs=MM_TOL
        )
        assert segments[2]["length_mm"] == pytest.approx(
            truth["tail"], abs=MM_TOL
        )

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0])
    def test_sense_agreement_false_reports_same_included_angle(
        self, mm_model, bend_angle
    ):
        """SenseAgreement=False must not flip the sweep to its reflex."""
        model, ctx = mm_model
        bar, truth = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=bend_angle, sense_agreement=False
        )
        arcs = arcs_of(get_bar_bend_info(bar))
        assert len(arcs) == 1
        assert arcs[0]["angle_deg"] == pytest.approx(
            truth["bend_angle_deg"], abs=ANGLE_TOL_DEG
        )
        assert arcs[0]["length_mm"] == pytest.approx(
            truth["arc_length"], abs=MM_TOL
        )

    def test_trim_values_in_degrees(self):
        """Models whose PLANEANGLEUNIT is a degree conversion unit."""
        model, ctx = ifc_builders.make_model(units="mm", angle_unit="degree")
        bar, truth = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=90.0, trim_in_degrees=True
        )
        arcs = arcs_of(get_bar_bend_info(bar))
        assert len(arcs) == 1
        assert arcs[0]["angle_deg"] == pytest.approx(90.0, abs=ANGLE_TOL_DEG)

    @pytest.mark.parametrize("bend_angle", [90.0, 135.0, 180.0])
    def test_both_curve_types_agree(self, mm_model, bend_angle):
        """The two encodings of the same hook must report the same numbers."""
        model, ctx = mm_model
        bar_idx, _ = ifc_builders.make_hook_bar_indexed(
            model, ctx, bend_angle_deg=bend_angle
        )
        bar_cmp, _ = ifc_builders.make_hook_bar_composite(
            model, ctx, bend_angle_deg=bend_angle
        )
        arc_idx = arcs_of(get_bar_bend_info(bar_idx))[0]
        arc_cmp = arcs_of(get_bar_bend_info(bar_cmp))[0]
        assert arc_idx["angle_deg"] == pytest.approx(
            arc_cmp["angle_deg"], abs=ANGLE_TOL_DEG
        )
        assert arc_idx["radius_mm"] == pytest.approx(
            arc_cmp["radius_mm"], abs=MM_TOL
        )
        assert arc_idx["length_mm"] == pytest.approx(
            arc_cmp["length_mm"], abs=MM_TOL
        )


# ─────────────────────────────────────────────────────────────────────
# Closed stirrup
# ─────────────────────────────────────────────────────────────────────
class TestClosedStirrup:

    def test_four_90_degree_corner_arcs(self, mm_model):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_closed_stirrup_bar(model, ctx)
        segments = get_bar_bend_info(bar)
        arcs = arcs_of(segments)
        assert len(arcs) == truth["n_arcs"]
        for arc in arcs:
            assert arc["angle_deg"] == pytest.approx(
                truth["corner_angle_deg"], abs=ANGLE_TOL_DEG
            )
            assert arc["radius_mm"] == pytest.approx(
                truth["corner_radius"], abs=MM_TOL
            )
            assert arc["length_mm"] == pytest.approx(
                truth["corner_arc_length"], abs=MM_TOL
            )

    def test_four_straight_edges_with_exact_lengths(self, mm_model):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_closed_stirrup_bar(
            model, ctx, width=200.0, height=300.0, corner_radius=20.0
        )
        straights = straights_of(get_bar_bend_info(bar))
        assert len(straights) == truth["n_straights"]
        measured = sorted(s["length_mm"] for s in straights)
        for got, expected in zip(measured, truth["edge_lengths"]):
            assert got == pytest.approx(expected, abs=MM_TOL)

    def test_alternating_segment_types(self, mm_model):
        model, ctx = mm_model
        bar, _ = ifc_builders.make_closed_stirrup_bar(model, ctx)
        types = [s["type"] for s in get_bar_bend_info(bar)]
        assert types == ["straight", "arc"] * 4


# ─────────────────────────────────────────────────────────────────────
# Straight bar / degenerate inputs
# ─────────────────────────────────────────────────────────────────────
class TestStraightAndDegenerate:

    def test_straight_bar_has_no_arcs(self, mm_model):
        model, ctx = mm_model
        bar, _ = ifc_builders.make_straight_bar(model, ctx, length=1200.0)
        segments = get_bar_bend_info(bar)
        assert segments, "straight bar should still yield straight segments"
        assert arcs_of(segments) == []

    def test_straight_bar_total_length(self, mm_model):
        model, ctx = mm_model
        bar, truth = ifc_builders.make_straight_bar(model, ctx, length=1200.0)
        total = sum(s["length_mm"] for s in get_bar_bend_info(bar))
        assert total == pytest.approx(truth["length"], abs=MM_TOL)

    def test_bar_without_representation_returns_empty(self, mm_model):
        model, _ = mm_model
        bar = ifc_builders.make_bare_bar(model, name="No geometry")
        assert get_bar_bend_info(bar) == []


# ─────────────────────────────────────────────────────────────────────
# unit_scale handling and the deprecated aliases
# ─────────────────────────────────────────────────────────────────────
class TestUnitScaleContract:

    def test_metre_model_with_explicit_unit_scale(self, m_model):
        """A physically identical hook authored in metres must produce
        the same *_mm values when unit_scale=1000 is passed."""
        model, ctx = m_model
        bar, truth = ifc_builders.make_hook_bar_indexed(
            model, ctx,
            bend_angle_deg=90.0, lead_in=200.0, tail=100.0, bend_radius=48.0,
            scale=0.001,  # author coordinates in metres
        )
        segments = get_bar_bend_info(bar, unit_scale=1000.0)
        arc = arcs_of(segments)[0]
        assert arc["angle_deg"] == pytest.approx(90.0, abs=ANGLE_TOL_DEG)
        assert arc["radius_mm"] == pytest.approx(48.0, abs=MM_TOL)
        assert arc["length_mm"] == pytest.approx(
            48.0 * math.pi / 2.0, abs=MM_TOL
        )
        assert segments[0]["length_mm"] == pytest.approx(200.0, abs=MM_TOL)
        assert segments[2]["length_mm"] == pytest.approx(100.0, abs=MM_TOL)

    def test_deprecated_length_alias_is_model_units(self, m_model):
        model, ctx = m_model
        bar, _ = ifc_builders.make_hook_bar_indexed(
            model, ctx, tail=100.0, scale=0.001
        )
        segments = get_bar_bend_info(bar, unit_scale=1000.0)
        # legacy "length" stays in model units (metres here)
        assert segments[-1]["length"] == pytest.approx(0.1, abs=0.0005)
        for seg in segments:
            assert seg["length_mm"] == pytest.approx(
                seg["length"] * 1000.0, rel=1e-9
            )
            assert seg["radius_mm"] == pytest.approx(
                seg["radius"] * 1000.0, rel=1e-9
            )

    def test_default_unit_scale_is_identity(self, mm_model):
        """With the default unit_scale=1.0, length_mm == length."""
        model, ctx = mm_model
        bar, _ = ifc_builders.make_hook_bar_indexed(model, ctx)
        for seg in get_bar_bend_info(bar):
            assert seg["length_mm"] == seg["length"]
            assert seg["radius_mm"] == seg["radius"]

    def test_every_segment_has_all_contract_keys(self, mm_model):
        model, ctx = mm_model
        bar_idx, _ = ifc_builders.make_hook_bar_indexed(model, ctx)
        bar_cmp, _ = ifc_builders.make_hook_bar_composite(model, ctx)
        required = {"type", "length_mm", "radius_mm", "angle_deg",
                    "length", "radius"}
        for bar in (bar_idx, bar_cmp):
            segments = get_bar_bend_info(bar)
            assert segments
            for seg in segments:
                assert required.issubset(seg.keys()), (
                    f"missing keys: {required - set(seg.keys())}"
                )
