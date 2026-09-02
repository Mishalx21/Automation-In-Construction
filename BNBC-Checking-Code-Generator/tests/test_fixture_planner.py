"""Planner validation: vocabulary enforcement, plan minimums, selector DSL."""

from __future__ import annotations

import pytest

from bnbc.contracts import FixtureSketch, SpecCard, SpecCondition, Verdict
from bnbc.fixtures import synthetic
from bnbc.fixtures.errors import PlanningError, TargetNotFoundError
from bnbc.fixtures.planner import build_selector, operator_vocabulary, plan_fixtures

BASE = ["D:/somewhere/base_model.ifc"]


def _card(sketches: list[FixtureSketch]) -> SpecCard:
    return SpecCard(
        rule_id="T.1",
        rule_title="toy rule",
        conditions=[SpecCondition(id="dia_min", requirement="diameter >= 12 mm")],
        fixture_sketches=sketches,
    )


def _valid_sketches() -> list[FixtureSketch]:
    tgt = {"ifc_class": "IfcReinforcingBar"}
    return [
        FixtureSketch(condition="dia_min", perturbation="dia 8", operator="set_nominal_diameter",
                      params={"diameter_mm": 8.0}, target=tgt),
        FixtureSketch(condition="dia_min", perturbation="dia 11.9 (boundary)",
                      operator="set_nominal_diameter", params={"diameter_mm": 11.9},
                      target=tgt, boundary=True),
        FixtureSketch(condition="dia_min", perturbation="dia 12.1 (edge pass)",
                      operator="set_nominal_diameter", params={"diameter_mm": 12.1},
                      target=tgt, expected_verdict=Verdict.PASS),
        FixtureSketch(condition="dia_min", perturbation="dia 20 (clear pass)",
                      operator="set_nominal_diameter", params={"diameter_mm": 20.0},
                      target=tgt, expected_verdict=Verdict.PASS),
        FixtureSketch(condition="dia_min", perturbation="diameter stripped",
                      operator="strip_attribute", params={"attribute": "NominalDiameter"},
                      target=tgt, expected_verdict=Verdict.UNKNOWN),
        FixtureSketch(condition="dia_min", perturbation="no bars",
                      operator="delete_elements_of_type",
                      params={"ifc_class": "IfcReinforcingBar"},
                      expected_verdict=Verdict.NOT_APPLICABLE),
    ]


class TestPlanFixtures:
    def test_valid_plan(self):
        specs = plan_fixtures(_card(_valid_sketches()), BASE)
        assert len(specs) == 6
        assert all(s.base_model == "base_model.ifc" for s in specs)
        assert specs[0].expected_conditions == ["dia_min"]
        assert specs[2].expected_conditions == []  # pass sketch names no kill target
        assert len({s.fixture_id for s in specs}) == 6
        assert len({s.file_name for s in specs}) == 6

    def test_unknown_operator_rejected(self):
        sketches = _valid_sketches()
        sketches[0].operator = "made_up_operator"
        with pytest.raises(PlanningError, match="unknown operator") as ei:
            plan_fixtures(_card(sketches), BASE)
        # Structured defects carry the sketch index (sketch-level repair input).
        assert any(d["sketch_index"] == 0 for d in ei.value.defects)

    def test_card_level_defects_have_null_index(self):
        sketches = [s for s in _valid_sketches() if s.expected_verdict != Verdict.PASS]
        with pytest.raises(PlanningError) as ei:
            plan_fixtures(_card(sketches), BASE)
        assert any(d["sketch_index"] is None for d in ei.value.defects)

    def test_missing_boundary_rejected(self):
        sketches = [s for s in _valid_sketches()]
        sketches[1].boundary = False
        with pytest.raises(PlanningError, match="boundary"):
            plan_fixtures(_card(sketches), BASE)

    def test_missing_unknown_variant_rejected_when_card_can_be_unknown(self):
        from bnbc.contracts import DataPrerequisite

        sketches = [s for s in _valid_sketches() if s.expected_verdict != Verdict.UNKNOWN]
        card = _card(sketches)
        card.prerequisites = [DataPrerequisite(
            condition="dia_min", entity="IfcReinforcingBar",
            requirement="NominalDiameter present",
            on_missing=Verdict.UNKNOWN,
        )]
        with pytest.raises(PlanningError, match="unknown"):
            plan_fixtures(card, BASE)

    def test_unknown_variant_not_required_for_total_fallback_cards(self):
        """A card whose prerequisites never yield unknown (total fallback
        conventions) must not be forced to author an unknown fixture — the
        forced fixture IS an oracle bug (observed live on 8.3.5.1)."""
        sketches = [s for s in _valid_sketches() if s.expected_verdict != Verdict.UNKNOWN]
        assert plan_fixtures(_card(sketches), BASE)  # no unknown prereq -> valid

    def test_missing_not_applicable_rejected(self):
        sketches = [s for s in _valid_sketches() if s.expected_verdict != Verdict.NOT_APPLICABLE]
        with pytest.raises(PlanningError, match="not_applicable"):
            plan_fixtures(_card(sketches), BASE)

    def test_no_fail_sketch_rejected(self):
        sketches = [s for s in _valid_sketches() if s.expected_verdict != Verdict.FAIL]
        with pytest.raises(PlanningError, match=">=1 violating"):
            plan_fixtures(_card(sketches), BASE)

    def test_single_fail_sketch_with_boundary_is_valid(self):
        """New budget policy: one kill fixture per condition is enough."""
        sketches = _valid_sketches()[1:]  # keep only the boundary fail sketch
        assert plan_fixtures(_card(sketches), BASE)

    def test_too_few_pass_sketches_rejected(self):
        sketches = _valid_sketches()
        del sketches[3]  # drop the second pass sketch, leaving one
        with pytest.raises(PlanningError, match=">=2 compliant"):
            plan_fixtures(_card(sketches), BASE)

    def test_fail_sketch_with_bad_condition_rejected(self):
        sketches = _valid_sketches()
        sketches[0].condition = "nonexistent_condition"
        with pytest.raises(PlanningError, match="must name a spec condition"):
            plan_fixtures(_card(sketches), BASE)

    def test_synthetic_base_requires_insert_operator(self):
        sketches = _valid_sketches()
        sketches[0].base_model = "__synthetic__"
        with pytest.raises(PlanningError, match="synthetic base"):
            plan_fixtures(_card(sketches), BASE)

    def test_multi_step_sketch_valid(self):
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0] = FixtureSketch(
            condition="dia_min", perturbation="insert then shorten",
            base_model="__synthetic__",
            steps=[
                FixtureStep(operator="insert_hooked_bar",
                            params={"bend_angle_deg": 180.0, "tail_mm": 100.0,
                                    "bar_name": "FIV chain bar"}),
                FixtureStep(operator="shorten_hook_tail",
                            params={"new_tail_mm": 40.0},
                            target={"ifc_class": "IfcReinforcingBar",
                                    "name_contains": "FIV chain"}),
            ],
        )
        specs = plan_fixtures(_card(sketches), BASE)
        assert len(specs[0].steps) == 2
        assert specs[0].operator == "insert_hooked_bar"  # first step names the file
        assert specs[0].file_name.startswith("F00_insert_hooked_bar_")
        assert specs[0].content_hash  # content-addressed for fixture reuse

    def test_step_with_unknown_operator_rejected(self):
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0] = FixtureSketch(
            condition="dia_min", perturbation="bad chain",
            steps=[FixtureStep(operator="set_nominal_diameter", params={"diameter_mm": 8.0},
                               target={"ifc_class": "IfcReinforcingBar"}),
                   FixtureStep(operator="made_up_operator")],
        )
        with pytest.raises(PlanningError, match="step 1: unknown operator"):
            plan_fixtures(_card(sketches), BASE)

    def test_redundant_operator_echoing_first_step_is_tolerated(self):
        """LLMs fill operator alongside steps; benign when it echoes step 0
        (observed live: a whole run rejected over this, 8.3.4.2 run 5)."""
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0].steps = [FixtureStep(
            operator="set_nominal_diameter", params={"diameter_mm": 8.0},
            target={"ifc_class": "IfcReinforcingBar"},
        )]  # sketch operator already == "set_nominal_diameter"
        specs = plan_fixtures(_card(sketches), BASE)
        assert specs[0].steps[0].operator == "set_nominal_diameter"

    def test_operator_label_is_inert_when_steps_present(self):
        """The sketch-level operator is a salient-step label once steps exist —
        steps fully determine the build (observed live: models label with
        mid-chain operators, not step 0; a plan must never fail over a label)."""
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0].steps = [FixtureStep(operator="strip_attribute",
                                         params={"attribute": "BarLength"},
                                         target={"ifc_class": "IfcReinforcingBar"})]
        # sketch operator field still says "set_nominal_diameter" — ignored
        specs = plan_fixtures(_card(sketches), BASE)
        assert specs[0].steps[0].operator == "strip_attribute"
        assert specs[0].operator == "strip_attribute"  # named from steps, not label

    def test_param_domain_violation_is_a_plan_defect(self):
        """Degenerate operator params fail at PLAN time with a sketch index
        (declarative PARAM_DOMAINS), not as a build-time crash."""
        sketches = _valid_sketches()
        sketches[0] = FixtureSketch(
            condition="dia_min", perturbation="degenerate hook",
            base_model="__synthetic__", operator="insert_hooked_bar",
            params={"bend_angle_deg": 0, "tail_mm": 0, "lead_in_mm": 0},
        )
        with pytest.raises(PlanningError, match="outside insert_hooked_bar domain") as ei:
            plan_fixtures(_card(sketches), BASE)
        assert any(d["sketch_index"] == 0 for d in ei.value.defects)
        assert "insert_straight_bar" in str(ei.value)  # the DOMAIN_NOTE guidance

    def test_synthetic_base_first_step_must_insert(self):
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0] = FixtureSketch(
            condition="dia_min", perturbation="bad synthetic chain",
            base_model="__synthetic__",
            steps=[FixtureStep(operator="shorten_hook_tail", params={"new_tail_mm": 40.0}),
                   FixtureStep(operator="insert_hooked_bar")],
        )
        with pytest.raises(PlanningError, match="FIRST step must be an"):
            plan_fixtures(_card(sketches), BASE)

    def test_vocabulary_lists_all_operators(self):
        vocab = operator_vocabulary()
        for name in ("shorten_hook_tail", "insert_hooked_bar", "strip_pset",
                     "delete_elements_of_type", "resize_profile",
                     "insert_straight_bar"):
            assert name in vocab


class TestDedupAndBudget:
    """Identical perturbations are ONE fixture; the plan has a hard budget
    (observed live: 8.3.4.2 authored 48 sketches incl. 9 identical NA ones)."""

    def _two_condition_card(self, sketches: list[FixtureSketch]) -> SpecCard:
        return SpecCard(
            rule_id="T.2",
            rule_title="toy rule",
            conditions=[
                SpecCondition(id="dia_min", requirement="diameter >= 12 mm"),
                SpecCondition(id="dia_max", requirement="diameter <= 40 mm"),
            ],
            fixture_sketches=sketches,
        )

    def _two_condition_sketches(self) -> list[FixtureSketch]:
        tgt = {"ifc_class": "IfcReinforcingBar"}
        return [
            FixtureSketch(condition="dia_min", perturbation="dia 8",
                          operator="set_nominal_diameter", params={"diameter_mm": 8.0},
                          target=tgt, boundary=True),
            FixtureSketch(condition="dia_max", perturbation="dia 50",
                          operator="set_nominal_diameter", params={"diameter_mm": 50.0},
                          target=tgt, boundary=True),
            FixtureSketch(condition="dia_min", perturbation="dia 20 (pass)",
                          operator="set_nominal_diameter", params={"diameter_mm": 20.0},
                          target=tgt, expected_verdict=Verdict.PASS),
            FixtureSketch(condition="dia_max", perturbation="dia 30 (pass)",
                          operator="set_nominal_diameter", params={"diameter_mm": 30.0},
                          target=tgt, expected_verdict=Verdict.PASS),
            FixtureSketch(condition="dia_min", perturbation="diameter stripped",
                          operator="strip_attribute", params={"attribute": "NominalDiameter"},
                          target=tgt, expected_verdict=Verdict.UNKNOWN),
            FixtureSketch(condition="dia_min", perturbation="no bars",
                          operator="delete_elements_of_type",
                          params={"ifc_class": "IfcReinforcingBar"},
                          expected_verdict=Verdict.NOT_APPLICABLE),
        ]

    def test_duplicate_na_sketches_merge_into_one_fixture(self):
        sketches = self._two_condition_sketches()
        sketches.append(FixtureSketch(
            condition="dia_max", perturbation="no bars (dup)",
            operator="delete_elements_of_type",
            params={"ifc_class": "IfcReinforcingBar"},
            expected_verdict=Verdict.NOT_APPLICABLE,
        ))
        specs = plan_fixtures(self._two_condition_card(sketches), BASE)
        na = [s for s in specs if s.expected_verdict == Verdict.NOT_APPLICABLE]
        assert len(na) == 1
        assert len(specs) == 6  # 7 sketches -> 6 fixtures

    def test_duplicate_fail_sketches_union_conditions(self):
        sketches = self._two_condition_sketches()
        # The dia_min kill fixture ALSO violates dia_max per a second sketch
        # with the identical perturbation.
        sketches.append(FixtureSketch(
            condition="dia_max", perturbation="dia 8 (also violates max? no — same content)",
            operator="set_nominal_diameter", params={"diameter_mm": 8.0},
            target={"ifc_class": "IfcReinforcingBar"},
        ))
        specs = plan_fixtures(self._two_condition_card(sketches), BASE)
        merged = [s for s in specs if s.params.get("diameter_mm") == 8.0]
        assert len(merged) == 1
        assert merged[0].expected_conditions == ["dia_min", "dia_max"]

    def test_contradictory_duplicate_verdicts_rejected(self):
        sketches = self._two_condition_sketches()
        sketches.append(FixtureSketch(
            condition="dia_min", perturbation="dia 8 but claimed pass",
            operator="set_nominal_diameter", params={"diameter_mm": 8.0},
            target={"ifc_class": "IfcReinforcingBar"},
            expected_verdict=Verdict.PASS,
        ))
        with pytest.raises(PlanningError, match="different verdict"):
            plan_fixtures(self._two_condition_card(sketches), BASE)

    def test_over_budget_plan_is_auto_trimmed(self, caplog):
        """Removal decisions never go to an LLM: over-budget plans are trimmed
        deterministically with the drops logged (observed live 2026-07-17: a
        bulk removal repair burned 22K reasoning tokens and died)."""
        import logging as _logging

        sketches = _valid_sketches()
        tgt = {"ifc_class": "IfcReinforcingBar"}
        for k in range(12):  # 12 more distinct fixtures blows the budget of 10
            sketches.append(FixtureSketch(
                condition="dia_min", perturbation=f"dia {k + 1}",
                operator="set_nominal_diameter", params={"diameter_mm": float(k + 1)},
                target=tgt))
        with caplog.at_level(_logging.INFO, logger="bnbc.fixtures.planner"):
            specs = plan_fixtures(_card(sketches), BASE)
        assert len(specs) == 10  # trimmed exactly to the budget
        verdicts = [s.expected_verdict for s in specs]
        # Coverage survives the trim: kill fixture, 2 pass, unknown, NA.
        assert verdicts.count(Verdict.FAIL) >= 1
        assert verdicts.count(Verdict.PASS) == 2
        assert Verdict.UNKNOWN in verdicts
        assert Verdict.NOT_APPLICABLE in verdicts
        # The boundary kill fixture is preferred over non-boundary variants.
        kept_fail_params = {s.params.get("diameter_mm") for s in specs
                            if s.expected_verdict == Verdict.FAIL}
        assert 11.9 in kept_fail_params  # the boundary=true sketch
        assert "Plan budget trim" in caplog.text  # no silent caps

    def test_boundary_not_required_on_large_rules(self):
        tgt = {"ifc_class": "IfcReinforcingBar"}
        conditions = [SpecCondition(id=f"c{k}", requirement=f"req {k}") for k in range(4)]
        sketches = [
            FixtureSketch(condition=f"c{k}", perturbation=f"viol {k}",
                          operator="set_nominal_diameter",
                          params={"diameter_mm": float(k + 1)}, target=tgt)
            for k in range(4)
        ] + [
            FixtureSketch(condition="c0", perturbation="pass a",
                          operator="set_nominal_diameter", params={"diameter_mm": 20.0},
                          target=tgt, expected_verdict=Verdict.PASS),
            FixtureSketch(condition="c0", perturbation="pass b",
                          operator="set_nominal_diameter", params={"diameter_mm": 25.0},
                          target=tgt, expected_verdict=Verdict.PASS),
            FixtureSketch(condition="c0", perturbation="stripped",
                          operator="strip_attribute", params={"attribute": "NominalDiameter"},
                          target=tgt, expected_verdict=Verdict.UNKNOWN),
            FixtureSketch(condition="c0", perturbation="no bars",
                          operator="delete_elements_of_type",
                          params={"ifc_class": "IfcReinforcingBar"},
                          expected_verdict=Verdict.NOT_APPLICABLE),
        ]
        card = SpecCard(rule_id="T.4", rule_title="big rule",
                        conditions=conditions, fixture_sketches=sketches)
        # No boundary=true anywhere — valid because the rule has 4 conditions.
        assert plan_fixtures(card, BASE)


class TestCensusValidation:
    """Dangling targets must fail at PLAN time via the base-model census,
    not minutes into the build (observed live: 8.3.4.2 run 4)."""

    @pytest.fixture
    def real_base(self, tmp_path):
        model, ctx = synthetic.make_model(units="mm", angle_unit="radian")
        synthetic.make_straight_bar(model, ctx, name="Rebar-01")
        path = tmp_path / "base.ifc"
        model.write(str(path))
        return str(path)

    def test_dangling_name_contains_fails_at_plan_time(self, real_base):
        sketches = _valid_sketches()
        sketches[0].target = {"ifc_class": "IfcReinforcingBar",
                              "name_contains": "FIV joint splice"}
        with pytest.raises(PlanningError, match="name contains") as ei:
            plan_fixtures(_card(sketches), [real_base])
        assert any(d["sketch_index"] == 0 for d in ei.value.defects)

    def test_missing_class_fails_at_plan_time(self, real_base):
        sketches = _valid_sketches()
        sketches[0].target = {"ifc_class": "IfcBeam"}
        with pytest.raises(PlanningError, match="no IfcBeam"):
            plan_fixtures(_card(sketches), [real_base])

    def test_index_out_of_range_fails_at_plan_time(self, real_base):
        sketches = _valid_sketches()
        sketches[0].target = {"ifc_class": "IfcReinforcingBar", "index": 5}
        with pytest.raises(PlanningError, match="out of range"):
            plan_fixtures(_card(sketches), [real_base])

    def test_name_contains_of_own_inserted_element_is_valid(self, real_base):
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sketches[0] = FixtureSketch(
            condition="dia_min", perturbation="insert then modify",
            steps=[
                FixtureStep(operator="insert_straight_bar",
                            params={"nominal_diameter_mm": 8.0,
                                    "bar_name": "FIV thin bar"}),
                FixtureStep(operator="set_nominal_diameter",
                            params={"diameter_mm": 8.0},
                            target={"ifc_class": "IfcReinforcingBar",
                                    "name_contains": "FIV thin"}),
            ],
        )
        assert plan_fixtures(_card(sketches), [real_base])  # no PlanningError

    def test_missing_base_file_skips_census_gracefully(self):
        # BASE points at a nonexistent path — census unavailable, plan valid.
        assert plan_fixtures(_card(_valid_sketches()), BASE)


class TestTargetClassInference:
    """A step target that names an element the SAME sketch inserted has an
    unambiguous ifc_class — infer it instead of rejecting the plan (observed
    live on 8.3.5.1: the omission recurred through two repair calls and a
    revision, burning the whole plan budget on a mechanical defect)."""

    def _sketch(self, target: dict) -> FixtureSketch:
        from bnbc.contracts import FixtureStep

        return FixtureSketch(
            condition="dia_min", perturbation="host + pset",
            base_model="__synthetic__",
            steps=[
                FixtureStep(operator="insert_host_element",
                            params={"ifc_class": "IfcColumn", "name": "FIV host column"}),
                FixtureStep(operator="set_pset_property",
                            params={"pset_name": "P", "property_name": "x", "value": 1.0},
                            target=target),
            ],
        )

    def test_class_inferred_from_own_host_insert(self):
        sketches = _valid_sketches()
        sketches[0] = self._sketch({"name_contains": "FIV host column"})
        specs = plan_fixtures(_card(sketches), BASE)
        spec = next(s for s in specs if s.operator == "insert_host_element")
        assert spec.steps[1].target["ifc_class"] == "IfcColumn"

    def test_class_inferred_for_inserted_bar(self):
        from bnbc.contracts import FixtureStep

        sketches = _valid_sketches()
        sk = self._sketch({"name_contains": "FIV host column"})
        sk.steps.append(FixtureStep(
            operator="set_nominal_diameter", params={"diameter_mm": 8.0},
            target={"name_contains": "FIV thin bar"}))
        sk.steps.insert(1, FixtureStep(
            operator="insert_straight_bar",
            params={"bar_name": "FIV thin bar", "nominal_diameter_mm": 8.0}))
        sketches[0] = sk
        specs = plan_fixtures(_card(sketches), BASE)
        spec = next(s for s in specs if s.operator == "insert_host_element")
        assert spec.steps[3].target["ifc_class"] == "IfcReinforcingBar"

    def test_uninferable_class_is_still_a_defect(self):
        sketches = _valid_sketches()
        sketches[0] = self._sketch({"name_contains": "no such element"})
        with pytest.raises(PlanningError, match="not inferable"):
            plan_fixtures(_card(sketches), BASE)


class TestSchemaAttributeValidation:
    """strip_attribute against an attribute the IFC schema does not declare
    must fail at PLAN time (observed live: a spec convention stripped
    'Thickness' off IfcWall and the build crashed terminally, 8.1.6.5)."""

    def test_nonexistent_attribute_is_a_plan_defect(self):
        sketches = _valid_sketches()
        sketches[4] = FixtureSketch(
            condition="dia_min", perturbation="strip fake attribute",
            operator="strip_attribute", params={"attribute": "Thickness"},
            target={"ifc_class": "IfcWall"},
            expected_verdict=Verdict.UNKNOWN,
        )
        with pytest.raises(PlanningError, match="declares no direct attribute") as ei:
            plan_fixtures(_card(sketches), BASE)
        assert any(d["sketch_index"] == 4 for d in ei.value.defects)

    def test_real_attribute_passes(self):
        sketches = _valid_sketches()
        # NominalDiameter IS a direct attribute of IfcReinforcingBar.
        assert plan_fixtures(_card(sketches), BASE)

    def test_build_failure_maps_to_sketch_defects(self, monkeypatch):
        """A FixtureBuildError during materialisation becomes a PlanningError
        with sketch-indexed defects — routed to plan repair, never terminal."""
        from bnbc.agent import fixtures_facade
        from bnbc.fixtures import manifest as manifest_mod
        from bnbc.fixtures.errors import FixtureBuildError

        def boom(card, planned, base_dir=None, out_dir=None):
            raise FixtureBuildError(
                "fixture T.1::F00 (set_nominal_diameter) failed to build: "
                "IfcReinforcingBar has no ObjectPlacement",
                fixture_id="T.1::F00", sketch_indices=[0],
            )

        monkeypatch.setattr(manifest_mod, "build_manifest", boom)
        card = _card(_valid_sketches())
        with pytest.raises(PlanningError, match="fixture build failed") as ei:
            fixtures_facade.ensure_fixtures(card, BASE)
        assert any(d["sketch_index"] == 0 for d in ei.value.defects)


class TestBuildSelector:
    def test_selects_by_name_and_index(self, mm_model):
        model, ctx = mm_model
        synthetic.make_straight_bar(model, ctx, name="Main bar A")
        target_bar, _ = synthetic.make_straight_bar(model, ctx, name="Main bar B")
        selector = build_selector({"ifc_class": "IfcReinforcingBar",
                                   "name_contains": "main bar", "index": 1})
        assert selector(model) == [target_bar]

    def test_selects_by_hook_angle(self, mm_model):
        model, ctx = mm_model
        synthetic.make_straight_bar(model, ctx)
        hooked, _ = synthetic.make_hook_bar_indexed(model, ctx, bend_angle_deg=180.0)
        selector = build_selector({"ifc_class": "IfcReinforcingBar",
                                   "with_hook_angle_deg": 180})
        assert selector(model) == [hooked]

    def test_all_flag(self, mm_model):
        model, ctx = mm_model
        synthetic.make_straight_bar(model, ctx)
        synthetic.make_straight_bar(model, ctx)
        selector = build_selector({"ifc_class": "IfcReinforcingBar", "all": True})
        assert len(selector(model)) == 2

    def test_out_of_range_index_raises(self, mm_model):
        model, ctx = mm_model
        synthetic.make_straight_bar(model, ctx)
        selector = build_selector({"ifc_class": "IfcReinforcingBar", "index": 5})
        with pytest.raises(TargetNotFoundError):
            selector(model)

    def test_missing_ifc_class_raises(self):
        with pytest.raises(PlanningError):
            build_selector({"name_contains": "bar"})

    def test_selects_by_guid_success(self, mm_model):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx, name="Main bar")
        guid = bar.GlobalId
        selector = build_selector({"ifc_class": "IfcReinforcingBar", "guid": guid})
        assert selector(model) == [bar]

        # Test global_id alias
        selector_alias = build_selector({"ifc_class": "IfcReinforcingBar", "global_id": guid})
        assert selector_alias(model) == [bar]

    def test_selects_by_guid_not_found_raises(self, mm_model):
        model, ctx = mm_model
        with pytest.raises(TargetNotFoundError, match="not found in model"):
            selector = build_selector({"ifc_class": "IfcReinforcingBar", "guid": "nonexistent_guid"})
            selector(model)

    def test_selects_by_guid_mismatch_class_raises(self, mm_model):
        model, ctx = mm_model
        bar, _ = synthetic.make_straight_bar(model, ctx, name="Main bar")
        guid = bar.GlobalId
        selector = build_selector({"ifc_class": "IfcWall", "guid": guid})
        with pytest.raises(TargetNotFoundError, match="found but is a IfcReinforcingBar, not IfcWall"):
            selector(model)
