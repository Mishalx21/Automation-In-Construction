"""End-to-end fixture engine test: base model -> plan -> manifest -> gate.

Toy rule: "reinforcing bars must have NominalDiameter >= 12 mm". A correct
checker must be accepted; an always-pass checker (the v1 vacuous-pass
failure mode) must be rejected with kill_rate 0.
"""

from __future__ import annotations

import json

import pytest

from bnbc.contracts import FixtureSketch, SpecCard, SpecCondition, Verdict
from bnbc.fixtures import synthetic
from bnbc.fixtures.gate import run_gate
from bnbc.fixtures.manifest import MANIFEST_FILENAME, build_manifest
from bnbc.fixtures.planner import plan_fixtures

pytestmark = pytest.mark.slow  # subprocess-per-fixture: seconds, not ms

TIMEOUT_S = 60

GOOD_CHECKER = '''\
def check_rule(model):
    bars = model.by_type("IfcReinforcingBar")
    checked, skipped, missing, locations = 0, 0, 0, []
    for bar in bars:
        dia = getattr(bar, "NominalDiameter", None)
        if dia is None:
            skipped += 1
            missing += 1
            continue
        checked += 1
        if float(dia) < 12.0:  # base model is authored in mm
            locations.append({
                "element": f"{bar.Name or 'Bar'} ({bar.GlobalId})",
                "storey": "Unknown level",
                "measured": f"dia={float(dia):.1f} mm, required=12.0 mm",
            })

    coverage = {"dia_min": {"elements_checked": checked,
                            "elements_skipped": skipped, "skip_reasons": {}}}
    if not bars:
        return {"verdict": "not_applicable", "violations": [], "violation_count": 0,
                "unknown_reasons": [], "checked_summary": coverage,
                "summary": "no reinforcing bars in model"}
    if missing:
        return {"verdict": "unknown", "violations": [], "violation_count": 0,
                "unknown_reasons": [{"condition": "dia_min",
                                     "missing": f"NominalDiameter on {missing} bar(s)",
                                     "affected_elements": missing}],
                "checked_summary": coverage, "summary": "diameter data missing"}
    if locations:
        return {"verdict": "fail",
                "violations": [{"condition": "dia_min",
                                "description": "bar diameter below minimum",
                                "rule_ref": "T.1", "threshold": ">= 12 mm",
                                "locations": locations}],
                "violation_count": len(locations), "unknown_reasons": [],
                "checked_summary": coverage,
                "summary": f"{len(locations)} undersized bar(s)"}
    return {"verdict": "pass", "violations": [], "violation_count": 0,
            "unknown_reasons": [], "checked_summary": coverage,
            "summary": f"all {checked} bar(s) comply"}
'''

# The v1 failure mode: always "pass", regardless of the model.
VACUOUS_CHECKER = '''\
def check_rule(model):
    n = len(model.by_type("IfcReinforcingBar"))
    return {"verdict": "pass", "violations": [], "violation_count": 0,
            "unknown_reasons": [],
            "checked_summary": {"dia_min": {"elements_checked": max(n, 1),
                                            "elements_skipped": 0, "skip_reasons": {}}},
            "summary": "looks fine"}
'''


@pytest.fixture(scope="module")
def toy_setup(tmp_path_factory):
    """Base model + spec card + built manifest, shared across gate tests."""
    root = tmp_path_factory.mktemp("fiv")
    base_dir, out_dir = root / "bases", root / "fixtures"
    base_dir.mkdir()

    model, ctx = synthetic.make_model(units="mm", angle_unit="radian")
    storey = synthetic.add_storey(model)
    bar, _ = synthetic.make_straight_bar(model, ctx, nominal_diameter=16.0)
    synthetic.contain_in_structure(model, storey, [bar])
    base_path = base_dir / "toy_base.ifc"
    model.write(str(base_path))

    tgt = {"ifc_class": "IfcReinforcingBar"}
    card = SpecCard(
        rule_id="T.1",
        rule_title="toy diameter rule",
        conditions=[SpecCondition(id="dia_min", requirement="bar diameter >= 12 mm")],
        fixture_sketches=[
            FixtureSketch(condition="dia_min", perturbation="dia 8",
                          operator="set_nominal_diameter", params={"diameter_mm": 8.0},
                          target=tgt),
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
                          operator="strip_attribute",
                          params={"attribute": "NominalDiameter"},
                          target=tgt, expected_verdict=Verdict.UNKNOWN),
            FixtureSketch(condition="dia_min", perturbation="no bars",
                          operator="delete_elements_of_type",
                          params={"ifc_class": "IfcReinforcingBar"},
                          expected_verdict=Verdict.NOT_APPLICABLE),
        ],
    )
    planned = plan_fixtures(card, [str(base_path)])
    manifest = build_manifest(card, planned, base_dir=base_dir, out_dir=out_dir)
    return card, manifest, out_dir


class TestManifest:
    def test_all_fixtures_built_and_selfverified(self, toy_setup):
        _card, manifest, out_dir = toy_setup
        assert len(manifest.fixtures) == 6
        for spec in manifest.fixtures:
            assert (out_dir / spec.file_name).exists()
            assert spec.self_verification is not None and spec.self_verification.ok
        fail_specs = [f for f in manifest.fixtures if f.expected_verdict == Verdict.FAIL]
        assert all(f.expected_elements for f in fail_specs)

    def test_manifest_json_written_and_reused(self, toy_setup):
        card, manifest, out_dir = toy_setup
        payload = json.loads((out_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert payload["spec_card_version"] == card.version
        # Same card version -> the manifest is reused, not rebuilt.
        again = build_manifest(card, [], base_dir=out_dir, out_dir=out_dir)
        assert [f.fixture_id for f in again.fixtures] == \
            [f.fixture_id for f in manifest.fixtures]


class TestGate:
    def test_correct_checker_accepted(self, toy_setup):
        _card, manifest, out_dir = toy_setup
        report = run_gate("T.1", GOOD_CHECKER, manifest, [], TIMEOUT_S,
                          fixtures_dir=out_dir)
        failed = [o for o in report.outcomes if not o.ok]
        assert report.accepted, f"gate rejected the correct checker: {failed}"
        assert report.kill_rate == 1.0

    def test_vacuous_checker_rejected(self, toy_setup):
        _card, manifest, out_dir = toy_setup
        report = run_gate("T.1", VACUOUS_CHECKER, manifest, [], TIMEOUT_S,
                          fixtures_dir=out_dir)
        assert not report.accepted
        assert report.kill_rate == 0.0
        # Both must-fail fixtures survived (that is exactly the v1 disease).
        missed = [o for o in report.outcomes
                  if o.expected_verdict == Verdict.FAIL and not o.ok]
        assert len(missed) == 2

    def test_crashing_checker_rejected(self, toy_setup):
        _card, manifest, out_dir = toy_setup
        report = run_gate("T.1", "def check_rule(model):\n    raise RuntimeError('boom')\n",
                          manifest, [], TIMEOUT_S, fixtures_dir=out_dir)
        assert not report.accepted
        assert all(o.execution_error for o in report.outcomes)

    def test_empty_manifest_never_accepts(self):
        from bnbc.contracts import FixtureManifest

        report = run_gate("T.1", GOOD_CHECKER,
                          FixtureManifest(rule_id="T.1", fixtures=[]), [], TIMEOUT_S,
                          fixtures_dir=".")
        assert not report.accepted
        assert report.static_errors
