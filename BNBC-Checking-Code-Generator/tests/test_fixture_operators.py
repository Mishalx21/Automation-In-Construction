"""Operator -> write -> selfverify round-trips on synthetic micro-models.

Every test applies an operator, writes the result to disk, and lets
``bnbc.fixtures.selfverify`` independently confirm the claimed change — the
same loop the manifest builder runs in production.
"""

from __future__ import annotations

import pytest

from bnbc.fixtures import measure, selfverify, synthetic
from bnbc.fixtures.errors import SelfVerificationError, TargetNotFoundError
from bnbc.fixtures.operators import OPERATORS


def _write_and_verify(tmp_path, result, name="fixture.ifc"):
    path = tmp_path / name
    result.model.write(str(path))
    return selfverify.verify_or_raise(path, result.expected)


def _add_placement(model, el, origin=(0.0, 0.0, 0.0)):
    axis = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=origin),
    )
    el.ObjectPlacement = model.create_entity("IfcLocalPlacement", RelativePlacement=axis)


class TestGeometryOperators:
    def test_shorten_hook_tail_indexed(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_hook_bar_indexed(model, ctx, bend_angle_deg=180.0, tail=100.0)
        result = OPERATORS["shorten_hook_tail"]().apply(model, bar.GlobalId, new_tail_mm=40.0)
        checks = _write_and_verify(tmp_path, result)
        assert all(c.ok for c in checks)
        # The input model is never mutated (operators are pure).
        assert measure.terminal_hook(bar).tail_mm == pytest.approx(100.0, abs=0.5)

    def test_lengthen_hook_tail_composite(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_hook_bar_composite(model, ctx, bend_angle_deg=90.0, tail=100.0)
        result = OPERATORS["shorten_hook_tail"]().apply(model, bar.GlobalId, new_tail_mm=250.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_set_arc_angle(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_hook_bar_indexed(model, ctx, bend_angle_deg=90.0)
        result = OPERATORS["set_arc_angle"]().apply(model, bar.GlobalId, target_angle_deg=135.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_translate_elements(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        _add_placement(model, bar, origin=(10.0, 20.0, 0.0))
        result = OPERATORS["translate_elements"]().apply(model, bar.GlobalId, dx_mm=25.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_copy_element_offset(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        _add_placement(model, bar)
        result = OPERATORS["copy_element_offset"]().apply(
            model, bar.GlobalId, offset_mm=(0.0, 30.0, 0.0)
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))
        assert len(result.affected_guids) == 2

    def test_resize_profile(self, mm_model, tmp_path):
        model, _ctx = mm_model
        profile = model.create_entity(
            "IfcRectangleProfileDef", ProfileType="AREA", XDim=300.0, YDim=400.0
        )
        solid = model.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            ExtrudedDirection=model.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
            ),
            Depth=3000.0,
        )
        shape = model.create_entity(
            "IfcShapeRepresentation", RepresentationIdentifier="Body",
            RepresentationType="SweptSolid", Items=[solid],
        )
        column = model.create_entity(
            "IfcColumn",
            GlobalId=synthetic._guid(),
            Name="Column",
            Representation=model.create_entity(
                "IfcProductDefinitionShape", Representations=[shape]
            ),
        )
        result = OPERATORS["resize_profile"]().apply(model, column.GlobalId, x_mm=250.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))
        # Shared-profile safety: the original profile entity is untouched.
        assert float(profile.XDim) == 300.0


class TestPropertyOperators:
    def test_translate_inserted_bar_without_placement(self, mm_model, tmp_path):
        """Operators are closed under composition: an inserted bar (no
        ObjectPlacement) is a valid translate/copy target — it gets an origin
        placement instead of crashing (observed live: 8.3.4.2 run 7)."""
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)  # no placement
        result = OPERATORS["translate_elements"]().apply(model, bar.GlobalId, dx_mm=25.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_copy_inserted_bar_without_placement(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)  # no placement
        result = OPERATORS["copy_element_offset"]().apply(
            model, bar.GlobalId, offset_mm=(40.0, 0.0, 0.0)
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_set_pset_property_create_and_update(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        result = OPERATORS["set_pset_property"]().apply(
            model, bar.GlobalId, pset_name="Pset_ConcreteElementGeneral",
            property_name="CompressiveStrength", value=25.0,
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))
        # Update path: same pset/property gets the new value, not a duplicate.
        result2 = OPERATORS["set_pset_property"]().apply(
            result.model, bar.GlobalId, pset_name="Pset_ConcreteElementGeneral",
            property_name="CompressiveStrength", value=30.0,
        )
        checks = _write_and_verify(tmp_path, result2, name="fixture2.ifc")
        assert all(c.ok for c in checks)

    def test_set_nominal_diameter(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx, nominal_diameter=16.0)
        result = OPERATORS["set_nominal_diameter"]().apply(model, bar.GlobalId, diameter_mm=8.0)
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_strip_attribute(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx, nominal_diameter=16.0)
        result = OPERATORS["strip_attribute"]().apply(
            model, bar.GlobalId, attribute="NominalDiameter"
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_strip_pset(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        synthetic.add_pset(model, bar, "Pset_ReinforcementBarPitch", {"Reference": "16mm"})
        result = OPERATORS["strip_pset"]().apply(
            model, bar.GlobalId, pset_name="Pset_ReinforcementBarPitch"
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_rename_material(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        material = model.create_entity("IfcMaterial", Name="Steel 420")
        model.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId=synthetic._guid(),
            RelatedObjects=[bar],
            RelatingMaterial=material,
        )
        result = OPERATORS["rename_material"]().apply(
            model, bar.GlobalId, new_name="Steel 300", old_name="Steel 420"
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_rename_material_missing_raises(self, mm_model):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        with pytest.raises(TargetNotFoundError):
            OPERATORS["rename_material"]().apply(model, bar.GlobalId, new_name="X")


class TestStructureOperators:
    def test_delete_elements(self, mm_model, tmp_path):
        model, ctx = mm_model
        keep, _ = synthetic.make_straight_bar(model, ctx, name="keep")
        doomed, _ = synthetic.make_straight_bar(model, ctx, name="doomed")
        storey = synthetic.add_storey(model)
        synthetic.contain_in_structure(model, storey, [keep, doomed])
        result = OPERATORS["delete_elements"]().apply(model, doomed.GlobalId)
        checks = _write_and_verify(tmp_path, result)
        assert all(c.ok for c in checks)

    def test_delete_elements_of_type(self, mm_model, tmp_path):
        model, ctx = mm_model
        synthetic.make_straight_bar(model, ctx)
        synthetic.make_straight_bar(model, ctx)
        result = OPERATORS["delete_elements_of_type"]().apply(
            model, None, ifc_class="IfcReinforcingBar"
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))


class TestInsertOperators:
    def test_insert_hooked_bar_indexed(self, mm_model, tmp_path):
        model, _ctx = mm_model
        synthetic.add_storey(model)
        result = OPERATORS["insert_hooked_bar"]().apply(
            model, None,
            bend_angle_deg=180.0, tail_mm=64.0, bend_radius_mm=48.0,
            nominal_diameter_mm=16.0, curve_style="indexed",
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_insert_hooked_bar_composite_metres(self, m_model, tmp_path):
        model, _ctx = m_model
        synthetic.add_storey(model)
        result = OPERATORS["insert_hooked_bar"]().apply(
            model, None,
            bend_angle_deg=90.0, tail_mm=150.0, bend_radius_mm=50.0,
            curve_style="composite",
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_insert_diameterless_bar(self, mm_model, tmp_path):
        model, _ctx = mm_model
        result = OPERATORS["insert_hooked_bar"]().apply(
            model, None, nominal_diameter_mm=None
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_insert_straight_bar(self, mm_model, tmp_path):
        model, _ctx = mm_model
        result = OPERATORS["insert_straight_bar"]().apply(
            model, None, length_mm=1200.0, nominal_diameter_mm=36.0
        )
        checks = _write_and_verify(tmp_path, result)
        assert all(c.ok for c in checks)

    def test_insert_host_element_beam(self, mm_model, tmp_path):
        model, _ctx = mm_model
        synthetic.add_storey(model)
        result = OPERATORS["insert_host_element"]().apply(
            model, None, ifc_class="IfcBeam",
            length_mm=3000.0, width_mm=300.0, height_mm=500.0,
            name="FIV host beam",
        )
        checks = _write_and_verify(tmp_path, result)
        assert all(c.ok for c in checks)
        beams = result.model.by_type("IfcBeam")
        assert len(beams) == 1
        assert beams[0].ObjectPlacement is not None  # composition closure (I11)

    def test_insert_host_element_column_axis_is_vertical(self, mm_model, tmp_path):
        """length_mm is the AXIS extent for every member class: a column is
        width x height in section, LENGTH tall (observed live: the old
        height-extruded mapping built 3-metre-wide slabs and poisoned every
        expected verdict on 8.3.5.1)."""
        model, _ctx = mm_model
        result = OPERATORS["insert_host_element"]().apply(
            model, None, ifc_class="IfcColumn",
            length_mm=3000.0, width_mm=300.0, height_mm=500.0,
            name="FIV host column",
        )
        checks = _write_and_verify(tmp_path, result)  # profile_dims = (300, 500)
        assert all(c.ok for c in checks)
        col = result.model.by_type("IfcColumn")[0]
        solid = col.Representation.Representations[0].Items[0]
        assert solid.SweptArea.XDim == 300.0
        assert solid.SweptArea.YDim == 500.0
        assert solid.Depth == 3000.0  # extruded vertically by length

    def test_insert_host_element_slab(self, mm_model, tmp_path):
        model, _ctx = mm_model
        result = OPERATORS["insert_host_element"]().apply(
            model, None, ifc_class="IfcSlab",
            length_mm=4000.0, width_mm=2000.0, height_mm=150.0,
            name="FIV host slab",
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))

    def test_insert_host_element_bad_class_is_plan_defect(self, mm_model):
        from bnbc.fixtures.errors import PlanningError

        model, _ctx = mm_model
        with pytest.raises(PlanningError, match="insert_host_element supports"):
            OPERATORS["insert_host_element"]().apply(
                model, None, ifc_class="IfcDoor"
            )

    def test_bar_aggregates_into_named_host(self, mm_model, tmp_path):
        """A synthetic compliant scene: host beam + bar related to it via
        IfcRelAggregates — what relationship-driven checkers look for."""
        model, _ctx = mm_model
        synthetic.add_storey(model)
        result = OPERATORS["insert_host_element"]().apply(
            model, None, ifc_class="IfcBeam", name="FIV host beam",
        )
        result2 = OPERATORS["insert_straight_bar"]().apply(
            result.model, None, length_mm=2800.0, bar_name="FIV bottom bar",
            host_name_contains="FIV host",
        )
        model = result2.model
        rels = model.by_type("IfcRelAggregates")
        pairs = [
            (r.RelatingObject.is_a(), [o.is_a() for o in r.RelatedObjects])
            for r in rels
        ]
        assert ("IfcBeam", ["IfcReinforcingBar"]) in pairs
        assert all(c.ok for c in _write_and_verify(tmp_path, result2))

    def test_copied_bar_shares_the_original_host(self, mm_model):
        """copy_element_offset must keep the copy in the SAME host as the
        original — a host-less copy makes every common-host layer/spacing
        convention read 'no layer' (observed live on 8.1.6.1)."""
        model, _ctx = mm_model
        synthetic.add_storey(model)
        r1 = OPERATORS["insert_host_element"]().apply(
            model, None, ifc_class="IfcBeam", name="FIV host beam")
        r2 = OPERATORS["insert_straight_bar"]().apply(
            r1.model, None, bar_name="FIV bar A", host_name_contains="FIV host")
        bar_a = next(b for b in r2.model.by_type("IfcReinforcingBar"))
        r3 = OPERATORS["copy_element_offset"]().apply(
            r2.model, bar_a.GlobalId, offset_mm=(0.0, 46.0, 0.0), new_name="FIV bar B")
        model = r3.model
        beam = model.by_type("IfcBeam")[0]
        hosted = {
            o.Name for rel in model.by_type("IfcRelAggregates")
            if rel.RelatingObject == beam for o in rel.RelatedObjects
        }
        assert {"FIV bar A", "FIV bar B"} <= hosted

    def test_bar_host_missing_is_plan_defect(self, mm_model):
        from bnbc.fixtures.errors import PlanningError

        model, _ctx = mm_model
        with pytest.raises(PlanningError, match="matches no host element"):
            OPERATORS["insert_straight_bar"]().apply(
                model, None, host_name_contains="FIV host",
            )

    def test_insert_hooked_bar_degenerate_params_are_a_plan_defect(self, mm_model):
        """bend_angle_deg=0 means 'no hook' — a plan defect pointing to
        insert_straight_bar, not a build-time self-verification crash
        (observed live on 8.3.4.2, 2026-07-16)."""
        from bnbc.fixtures.errors import PlanningError

        model, _ctx = mm_model
        with pytest.raises(PlanningError, match="insert_straight_bar"):
            OPERATORS["insert_hooked_bar"]().apply(
                model, None, bend_angle_deg=0, lead_in_mm=0, tail_mm=0
            )

    def test_multi_step_chain_builds_and_selfverifies(self, tmp_path):
        """Insert a hooked bar, then shorten its tail in a second step — the
        merged expectations must verify the FINAL state (tail 40, not the
        insert's 100)."""
        import ifcopenshell

        from bnbc.contracts import FixtureSpec, FixtureStep, SpecCard, Verdict
        from bnbc.fixtures import manifest as manifest_mod

        spec = FixtureSpec(
            fixture_id="T.1::F00", rule_id="T.1", base_model="__synthetic__",
            operator="insert_hooked_bar",
            steps=[
                FixtureStep(operator="insert_hooked_bar",
                            params={"bend_angle_deg": 180.0, "tail_mm": 100.0,
                                    "bar_name": "FIV chain bar"}),
                FixtureStep(operator="shorten_hook_tail",
                            params={"new_tail_mm": 40.0},
                            target={"ifc_class": "IfcReinforcingBar",
                                    "name_contains": "FIV chain"}),
            ],
            file_name="F00_chain.ifc", expected_verdict=Verdict.FAIL,
            expected_conditions=["c1"],
        )
        built = manifest_mod.build_manifest(SpecCard(rule_id="T.1"), [spec], out_dir=tmp_path)
        assert built.fixtures[0].self_verification.ok

        bar = ifcopenshell.open(str(tmp_path / "F00_chain.ifc")).by_type("IfcReinforcingBar")[0]
        hook = measure.terminal_hook(bar)
        assert hook.tail_mm == pytest.approx(40.0, abs=0.5)

    def test_synthetic_compliant_scene_builds_and_selfverifies(self, tmp_path):
        """The C1 fix end-to-end: a compliant scene fixture (host beam + two
        bars related to it) built on a __synthetic__ base — the expected
        verdict is sound by construction because the scene contains ONLY what
        the sketch inserted."""
        import ifcopenshell

        from bnbc.contracts import FixtureSpec, FixtureStep, SpecCard, Verdict
        from bnbc.fixtures import manifest as manifest_mod

        spec = FixtureSpec(
            fixture_id="T.1::F00", rule_id="T.1", base_model="__synthetic__",
            operator="insert_host_element",
            steps=[
                FixtureStep(operator="insert_host_element",
                            params={"ifc_class": "IfcBeam", "length_mm": 3000.0,
                                    "width_mm": 300.0, "height_mm": 500.0,
                                    "name": "FIV host beam"}),
                FixtureStep(operator="insert_straight_bar",
                            params={"length_mm": 2800.0, "nominal_diameter_mm": 16.0,
                                    "bar_name": "FIV bar A",
                                    "host_name_contains": "FIV host"}),
                FixtureStep(operator="insert_straight_bar",
                            params={"length_mm": 2800.0, "nominal_diameter_mm": 16.0,
                                    "bar_name": "FIV bar B",
                                    "host_name_contains": "FIV host"}),
            ],
            file_name="F00_scene.ifc", expected_verdict=Verdict.PASS,
        )
        built = manifest_mod.build_manifest(SpecCard(rule_id="T.1"), [spec], out_dir=tmp_path)
        assert built.fixtures[0].self_verification.ok

        scene = ifcopenshell.open(str(tmp_path / "F00_scene.ifc"))
        assert len(scene.by_type("IfcBeam")) == 1
        assert len(scene.by_type("IfcReinforcingBar")) == 2
        assert len(scene.by_type("IfcRelAggregates")) >= 2  # both bars in the host

    def test_unchanged_fixture_is_reused_across_rebuilds(self, tmp_path, caplog):
        """Content-hashed fixtures survive card regenerations: same
        (base_model, steps) chain -> the built, self-verified file is reused
        instead of rebuilt."""
        import logging as _logging

        from bnbc.contracts import FixtureSpec, FixtureStep, SpecCard, Verdict
        from bnbc.fixtures import manifest as manifest_mod

        spec = FixtureSpec(
            fixture_id="T.1::F00", rule_id="T.1", base_model="__synthetic__",
            operator="insert_straight_bar",
            steps=[FixtureStep(operator="insert_straight_bar",
                               params={"length_mm": 900.0})],
            content_hash="abc123def0",
            file_name="F00_insert_straight_bar_abc123def0.ifc",
            expected_verdict=Verdict.FAIL, expected_conditions=["c1"],
        )
        manifest_mod.build_manifest(SpecCard(rule_id="T.1"), [spec], out_dir=tmp_path)
        with caplog.at_level(_logging.INFO, logger="bnbc.fixtures.manifest"):
            manifest_mod.build_manifest(
                SpecCard(rule_id="T.1", version=2), [spec.model_copy(deep=True)],
                out_dir=tmp_path,
            )
        assert any("Reused fixture" in r.message for r in caplog.records)

    def test_insert_closed_stirrup_has_no_terminal_hooks(self, mm_model, tmp_path):
        model, _ctx = mm_model
        synthetic.add_storey(model)
        result = OPERATORS["insert_closed_stirrup"]().apply(
            model, None, width_mm=200.0, height_mm=300.0, corner_radius_mm=20.0
        )
        assert all(c.ok for c in _write_and_verify(tmp_path, result))


class TestSelfVerifyCatchesLies:
    def test_wrong_expectation_fails_verification(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_hook_bar_indexed(model, ctx, tail=100.0)
        result = OPERATORS["shorten_hook_tail"]().apply(model, bar.GlobalId, new_tail_mm=40.0)
        # Corrupt the claim: pretend the tail should still be 100 mm.
        result.expected[0]["value_mm"] = 100.0
        path = tmp_path / "lie.ifc"
        result.model.write(str(path))
        with pytest.raises(SelfVerificationError):
            selfverify.verify_or_raise(path, result.expected)

    def test_unknown_check_kind_is_a_failure(self, mm_model, tmp_path):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx)
        path = tmp_path / "m.ifc"
        model.write(str(path))
        results = selfverify.verify_file(path, [{"check": "does_not_exist"}])
        assert len(results) == 1 and not results[0].ok
