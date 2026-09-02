"""Agent tests: fault classification, LLM-layer robustness, the drafter tool
session, and full graph runs (accept path, reject path, spec-revision outer
loop, plan repair) against a fake LLM provider and a monkeypatched fixture
facade — no network, no ifcopenshell subprocesses.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bnbc import config as cfg
from bnbc import llm
from bnbc.agent import fixtures_facade
from bnbc.agent.conformance import ConformanceReview
from bnbc.agent.execution import classify_fault, parse_ast_limitations
from bnbc.agent.graph import run_rule
from bnbc.contracts import (
    AcceptanceReport,
    FixtureManifest,
    FixtureOutcome,
    FixtureSketch,
    FixtureSpec,
    SpecCard,
    SpecCondition,
    Verdict,
)
from bnbc.llm.base import LLMProvider, LLMResponse, ToolCall, Usage

USAGE = Usage(input_tokens=100, output_tokens=50)


# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

class FakeProvider(LLMProvider):
    """Answers from a caller-supplied ``reply(model, structured, tools)`` hook.

    The hook returns a string (assistant text), a pydantic model (structured
    reply), a list of ToolCall, or an exception instance to raise.
    """

    name = "fake"

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[dict] = []

    async def generate(self, *, model, messages, temperature=0.0, max_output_tokens=0,
                       structured=None, tools=None, reasoning_effort=""):
        self.calls.append({"model": model, "structured": structured, "tools": tools})
        payload = self._reply(model, structured, tools)
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, str):
            return LLMResponse(text=payload, usage=USAGE, model=model)
        if isinstance(payload, list):
            return LLMResponse(tool_calls=tuple(payload), usage=USAGE, model=model)
        return LLMResponse(text="", parsed=payload, usage=USAGE, model=model)


def install(reply) -> FakeProvider:
    provider = FakeProvider(reply)
    llm.set_provider_for_testing(provider)
    return provider


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    llm.reset_provider()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def _outcome(i: int, expected: Verdict, actual: Verdict | None, ok: bool) -> FixtureOutcome:
    return FixtureOutcome(
        fixture_id=f"R::F{i:02d}", expected_verdict=expected, actual_verdict=actual, ok=ok
    )


class TestFaultClassification:
    def test_uniform_not_applicable_is_oracle_fault(self):
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, Verdict.NOT_APPLICABLE, False),
            _outcome(1, Verdict.FAIL, Verdict.NOT_APPLICABLE, False),
            _outcome(2, Verdict.PASS, Verdict.NOT_APPLICABLE, False),
        ])
        assert classify_fault(report) == "oracle"

    def test_uniform_unknown_is_oracle_fault(self):
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, Verdict.UNKNOWN, False),
            _outcome(1, Verdict.PASS, Verdict.UNKNOWN, False),
        ])
        assert classify_fault(report) == "oracle"

    def test_mixed_wrong_verdicts_are_code_fault(self):
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, Verdict.NOT_APPLICABLE, False),
            _outcome(1, Verdict.FAIL, Verdict.PASS, False),
        ])
        assert classify_fault(report) == "code"

    def test_partial_misses_below_ratio_are_code_fault(self):
        # 1 of 3 scored fixtures missed (< ORACLE_FAULT_MISS_RATIO).
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, Verdict.FAIL, True),
            _outcome(1, Verdict.PASS, Verdict.PASS, True),
            _outcome(2, Verdict.FAIL, Verdict.NOT_APPLICABLE, False),
        ])
        assert classify_fault(report) == "code"

    def test_no_misses_is_code_fault(self):
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, Verdict.FAIL, True),
        ])
        assert classify_fault(report) == "code"

    def test_execution_errors_break_uniformity(self):
        # actual_verdict None (crash) is not a scope decision -> code fault.
        report = AcceptanceReport(rule_id="R", outcomes=[
            _outcome(0, Verdict.FAIL, None, False),
            _outcome(1, Verdict.FAIL, Verdict.NOT_APPLICABLE, False),
        ])
        assert classify_fault(report) == "code"


class TestLLMLayer:
    """Regression tests for the runaway-call and lost-accounting incidents."""

    def test_wall_timeout_fails_fast_without_retry(self, monkeypatch):
        from bnbc.llm import LLMWallTimeout, TokenMeter, call_llm

        # Small but non-zero: the call must actually start before the ceiling
        # fires, otherwise the test would pass without exercising anything.
        monkeypatch.setattr(cfg, "LLM_WALL_TIMEOUT_SECONDS", 0.05)

        class _Slow(LLMProvider):
            name = "slow"
            attempts = 0

            async def generate(self, **kwargs):
                type(self).attempts += 1
                await asyncio.sleep(5)

        llm.set_provider_for_testing(_Slow())
        meter = TokenMeter(budget=10**9, alert=10**9)
        with pytest.raises(LLMWallTimeout, match="wall-clock"):
            asyncio.run(call_llm("m", [], meter, "draft", structured=SpecCard))
        assert _Slow.attempts == 1  # no schema-less retry after a wall timeout

    def test_failed_calls_are_metered(self):
        """A structured reply that burned tokens but did not parse must still
        count against the budget — and so must the schema-less retry."""
        from bnbc.llm import LLMOutputError, TokenMeter, call_llm

        class _Bad(LLMProvider):
            name = "bad"

            async def generate(self, *, structured=None, **kwargs):
                if structured is not None:
                    raise LLMOutputError("did not validate", call_usage=USAGE.as_dict())
                return LLMResponse(text="not json at all", usage=USAGE, model="m")

        llm.set_provider_for_testing(_Bad())
        meter = TokenMeter(budget=10**9, alert=10**9)
        with pytest.raises(LLMOutputError):
            asyncio.run(call_llm("m", [], meter, "spec_card", structured=SpecCard))
        assert meter.total == 300  # failed structured call + failed retry

    def test_unpriced_model_is_flagged_not_silently_free(self):
        """A model with no listed price must not make a run report $0.0000 —
        that number ends up in a paper."""
        from bnbc.llm import TokenMeter

        meter = TokenMeter(budget=10**9, alert=10**9)
        meter.add("draft", {"input_tokens": 1000, "output_tokens": 500},
                  model_name="some-brand-new-model")
        state = meter.to_state()
        assert state["cost_is_partial"] is True
        assert state["unpriced_models"] == ["some-brand-new-model"]

        priced = TokenMeter(budget=10**9, alert=10**9)
        priced.add("draft", {"input_tokens": 10**6, "output_tokens": 0},
                   model_name="gemini-2.5-flash")
        assert priced.to_state()["total_cost_usd"] == 0.3
        assert "cost_is_partial" not in priced.to_state()

    def test_schema_less_retry_recovers_a_structured_call(self):
        """Providers disagree about schema dialects; the output contract does
        not — a native-schema failure must still yield a validated object."""
        from bnbc.llm import LLMOutputError, TokenMeter, call_llm

        card_json = _spec_card("R.1").model_dump_json()

        class _NoNativeSchema(LLMProvider):
            name = "no-schema"

            async def generate(self, *, structured=None, **kwargs):
                if structured is not None:
                    raise LLMOutputError("schema dialect rejected")
                return LLMResponse(text=card_json, usage=USAGE, model="m")

        llm.set_provider_for_testing(_NoNativeSchema())
        meter = TokenMeter(budget=10**9, alert=10**9)
        parsed = asyncio.run(call_llm("m", [], meter, "spec_card", structured=SpecCard))
        assert parsed.rule_id == "R.1"


class TestGeminiKeyPool:
    def test_parses_every_key_list_shape(self):
        from bnbc.llm.gemini import parse_api_keys

        assert parse_api_keys("a,b, c") == ["a", "b", "c"]
        assert parse_api_keys('["a", "b"]') == ["a", "b"]
        assert parse_api_keys("[a, b]") == ["a", "b"]
        assert parse_api_keys("solo") == ["solo"]
        assert parse_api_keys("") == []

    def test_rotation_skips_dead_keys_and_gives_up_when_all_dead(self):
        from bnbc.llm.base import LLMProviderError
        from bnbc.llm.gemini import KeyPool

        pool = KeyPool(["k1", "k2"])
        pool.mark_dead(pool.current())
        assert pool.current()  # the surviving key
        pool.mark_dead(pool.current())
        with pytest.raises(LLMProviderError, match="invalid"):
            pool.current()

    def test_empty_pool_is_a_configuration_error(self):
        from bnbc.llm.base import LLMProviderError
        from bnbc.llm.gemini import KeyPool

        with pytest.raises(LLMProviderError, match="GEMINI_API_KEYS"):
            KeyPool([])


class TestExemplarRetrieval:
    def _store(self, tmp_path, rule_id: str, requirement: str):
        """An artifacts dir holding one gate-accepted rule."""
        from bnbc.agent.rule_store import RuleStore

        rdir = tmp_path / rule_id
        rdir.mkdir(parents=True)
        (rdir / "checker.py").write_text("def check_rule(m): ...\n", encoding="utf-8")
        (rdir / "acceptance.json").write_text(json.dumps({"accepted": True}), encoding="utf-8")
        (rdir / "spec_card.yaml").write_text(
            f'rule_id: {rule_id}\nrule_title: hooks\nconditions:\n'
            f'  - id: c\n    requirement: {requirement}\n',
            encoding="utf-8",
        )
        return RuleStore(tmp_path)

    def _card(self, rule_id: str, requirement: str,
              title: str = "standard hooks", cond_id: str = "hook_extension_min") -> dict:
        return {"rule_id": rule_id, "rule_title": title,
                "conditions": [{"id": cond_id, "requirement": requirement}]}

    def test_retrieves_a_similar_accepted_rule(self, tmp_path):
        from bnbc.agent import retrieval

        store = self._store(tmp_path, "X.1", "hook extension minimum reinforcing bars")
        found = retrieval.retrieve_exemplars(
            self._card("Y.9", "hook extension minimum reinforcing bars"), store
        )
        assert [e["rule_id"] for e in found] == ["X.1"]

    def test_never_retrieves_the_rule_itself(self, tmp_path):
        """A rule shown its own accepted checker would be copying, not solving."""
        from bnbc.agent import retrieval

        store = self._store(tmp_path, "X.1", "hook extension minimum reinforcing bars")
        assert retrieval.retrieve_exemplars(
            self._card("X.1", "hook extension minimum reinforcing bars"), store
        ) == []

    def test_irrelevant_exemplars_are_filtered_out(self, tmp_path):
        """Observed live: a hook checker retrieved for a stirrup rule added 8 KB
        of misleading code. The relevance floor is what stops that."""
        from bnbc.agent import retrieval

        store = self._store(tmp_path, "X.1", "hook extension minimum reinforcing bars")
        assert retrieval.retrieve_exemplars(
            self._card("Y.9", "minimum concrete cover over foundation footings",
                       title="concrete cover", cond_id="cover_depth_min"),
            store,
        ) == []

    def test_exemplars_reach_the_drafter_brief(self):
        """Retrieval that nothing reads is wasted work — the opening brief
        must actually carry the exemplars."""
        from bnbc.agent.agentic_draft import _opening_brief

        state = {
            "rule_id": "R.1",
            "spec_card": _spec_card("R.1").model_dump(mode="json"),
            "exemplars": [{"rule_id": "X.1", "spec_summary": "hook rule",
                           "code": "def check_rule(m): ..."}],
            "fixture_manifest": _manifest("R.1").model_dump(mode="json"),
        }
        brief = _opening_brief(state)
        assert "Exemplars" in brief
        assert "def check_rule(m): ..." in brief


class TestStaticGate:
    def test_flags_sampling_and_silent_except(self):
        code = (
            "def check_rule(m):\n    xs = m.by_type('X')[:50]\n"
            "    try:\n        pass\n    except Exception:\n        pass\n"
        )
        errors = parse_ast_limitations(code)
        assert any("slicing" in e for e in errors)
        assert any("silent" in e for e in errors)


class TestAgentTools:
    def test_inspect_snippet_safety(self):
        from bnbc.agent.tools import inspect_snippet_errors

        assert inspect_snippet_errors("print(len(model.by_type('IfcBeam')))") == []
        assert inspect_snippet_errors("import os; os.remove('x')")
        assert inspect_snippet_errors("open('x', 'w')")
        assert inspect_snippet_errors("def broken(:")

    def test_unknown_tool_is_reported_not_raised(self):
        from bnbc.agent.tools import ToolContext, dispatch

        result = dispatch(ToolContext(state={}), ToolCall(id="1", name="nope", args={}))
        assert "unknown tool" in result

    def test_tool_crash_becomes_feedback(self):
        from bnbc.agent.tools import ToolContext, dispatch

        # No rule_id in state -> the evaluate handler raises; the session must
        # see feedback rather than lose the run.
        result = dispatch(
            ToolContext(state={}), ToolCall(id="1", name="evaluate", args={"code": "x = 1"})
        )
        assert result.startswith("tool evaluate failed")


class TestElitism:
    """Rewrites oscillate (observed live: 6/11 -> 5/11) — the best candidate
    of the run is kept and handed to the human on terminal rejection."""

    def _cand(self, ok_count: int, kill: float, code: str = "code") -> dict:
        outcomes = [
            {"fixture_id": f"R.1::F{i:02d}", "expected_verdict": "fail",
             "actual_verdict": "fail" if i < ok_count else "pass",
             "conditions_hit": [], "conditions_missed": [],
             "elements_hit": [], "elements_missed": [],
             "execution_error": "", "ok": i < ok_count}
            for i in range(5)
        ]
        return {"candidate_id": "m", "model_name": "m", "code": code,
                "kill_rate": kill, "acceptance": {"outcomes": outcomes}}

    def test_candidate_score_orders_by_fixtures_then_kill(self):
        from bnbc.agent.execution import candidate_score

        assert candidate_score(self._cand(3, 0.5)) > candidate_score(self._cand(2, 0.9))
        assert candidate_score(self._cand(2, 0.9)) > candidate_score(self._cand(2, 0.5))
        assert candidate_score(None) == (0, 0.0)

    def test_best_of_keeps_better_previous(self):
        from bnbc.agent.execution import best_of, candidate_score

        best = best_of(self._cand(2, 0.3, "worse"), None)
        assert best["code"] == "worse"
        best = best_of(self._cand(4, 0.7, "better"), best)
        assert best["code"] == "better"
        best = best_of(self._cand(1, 0.1, "regressed"), best)  # no displacement
        assert best["code"] == "better"
        assert candidate_score(best) == (4, 0.7)

    def test_reject_stores_best_of_run_with_human_tag(self, tmp_path):
        from bnbc.agent.rule_store import reject_node

        state = {
            "rule_id": "R.1",
            "artifacts_dir": str(tmp_path),
            "status": "executed",
            "candidate": self._cand(1, 0.1, "regressed final code"),
            "best_candidate": self._cand(4, 0.7, "best code of the run"),
        }
        asyncio.run(reject_node(state))
        rejection = json.loads((tmp_path / "R.1" / "rejection.json").read_text(encoding="utf-8"))
        assert rejection["needs_human_intervention"] is True
        assert rejection["candidate_is_best_of_run"] is True
        assert rejection["candidate"]["code"] == "best code of the run"
        draft = (tmp_path / "R.1" / "partial" / "checker.draft.py").read_text(encoding="utf-8")
        assert draft == "best code of the run"
        summary = json.loads(
            (tmp_path / "R.1" / "partial" / "draft_summary.json").read_text(encoding="utf-8")
        )
        assert summary["needs_human_intervention"] is True
        assert summary["fixtures_ok"] == 4


class TestPlannerConventionConsistency:
    def test_gating_convention_without_discharge_is_plan_invalid(self):
        from bnbc.contracts import MeasurementConvention
        from bnbc.fixtures.errors import PlanningError
        from bnbc.fixtures.planner import plan_fixtures

        card = _spec_card("R.1")
        card.conventions = [MeasurementConvention(
            topic="smf tagging", decision="only explicitly tagged members apply",
            gates_applicability=True,  # no fixture_discharge
        )]
        with pytest.raises(PlanningError, match="fixture_discharge"):
            plan_fixtures(card, ["b.ifc"])

    def test_gating_convention_with_discharge_passes(self):
        from bnbc.contracts import MeasurementConvention
        from bnbc.fixtures.planner import plan_fixtures

        card = _spec_card("R.1")
        card.conventions = [MeasurementConvention(
            topic="smf tagging", decision="only explicitly tagged members apply",
            gates_applicability=True,
            fixture_discharge="all fail/pass sketches use insert_* elements named FIV-SMF",
        )]
        assert plan_fixtures(card, ["b.ifc"])  # no PlanningError


# ---------------------------------------------------------------------------
# Full graph runs
# ---------------------------------------------------------------------------

GOOD_CODE = '''\
def check_rule(model):
    return {"verdict": "pass", "violations": [], "violation_count": 0,
            "unknown_reasons": [],
            "checked_summary": {"c1": {"elements_checked": 3,
                                       "elements_skipped": 0, "skip_reasons": {}}},
            "summary": "ok"}
'''

PREREQS = "PREREQUISITES = []\n"
DRAFT_REPLY = f"```python\n{GOOD_CODE}{PREREQS}```"


def _spec_card(rule_id: str) -> SpecCard:
    return SpecCard(
        rule_id=rule_id,
        rule_title="fake rule",
        conditions=[SpecCondition(id="c1", requirement="something >= threshold")],
        fixture_sketches=[
            FixtureSketch(condition="c1", perturbation="viol", operator="insert_hooked_bar",
                          base_model="__synthetic__", params={"tail_mm": 5.0}),
            FixtureSketch(condition="c1", perturbation="boundary", operator="insert_hooked_bar",
                          base_model="__synthetic__", params={"tail_mm": 15.0}, boundary=True),
            FixtureSketch(condition="c1", perturbation="edge pass", operator="insert_hooked_bar",
                          base_model="__synthetic__", params={"tail_mm": 22.0},
                          expected_verdict=Verdict.PASS),
            FixtureSketch(condition="c1", perturbation="clear pass", operator="insert_hooked_bar",
                          base_model="__synthetic__", params={"tail_mm": 30.0},
                          expected_verdict=Verdict.PASS),
            FixtureSketch(condition="c1", perturbation="unknown", operator="insert_hooked_bar",
                          base_model="__synthetic__", params={"nominal_diameter_mm": None},
                          expected_verdict=Verdict.UNKNOWN),
            FixtureSketch(condition="c1", perturbation="na", operator="delete_elements_of_type",
                          params={"ifc_class": "IfcReinforcingBar"},
                          expected_verdict=Verdict.NOT_APPLICABLE),
        ],
    )


def _manifest(rule_id: str) -> FixtureManifest:
    return FixtureManifest(rule_id=rule_id, fixtures=[
        FixtureSpec(fixture_id=f"{rule_id}::F00", rule_id=rule_id, base_model="b.ifc",
                    operator="insert_hooked_bar", file_name="F00.ifc",
                    expected_verdict=Verdict.FAIL, expected_conditions=["c1"]),
    ])


def _gate_report(rule_id: str, ok: bool) -> AcceptanceReport:
    return AcceptanceReport(
        rule_id=rule_id,
        outcomes=[FixtureOutcome(
            fixture_id=f"{rule_id}::F00", expected_verdict=Verdict.FAIL,
            actual_verdict=Verdict.FAIL if ok else Verdict.PASS,
            conditions_hit=["c1"] if ok else [],
            conditions_missed=[] if ok else ["c1"],
            ok=ok,
        )],
        accepted=ok,
    )


def _gate_report_na(rule_id: str) -> AcceptanceReport:
    """Oracle-fault signature: every scored fixture returns not_applicable."""
    return AcceptanceReport(
        rule_id=rule_id,
        outcomes=[
            FixtureOutcome(fixture_id=f"{rule_id}::F00", expected_verdict=Verdict.FAIL,
                           actual_verdict=Verdict.NOT_APPLICABLE,
                           conditions_missed=["c1"], ok=False),
            FixtureOutcome(fixture_id=f"{rule_id}::F01", expected_verdict=Verdict.FAIL,
                           actual_verdict=Verdict.NOT_APPLICABLE,
                           conditions_missed=["c1"], ok=False),
            FixtureOutcome(fixture_id=f"{rule_id}::F02", expected_verdict=Verdict.PASS,
                           actual_verdict=Verdict.NOT_APPLICABLE, ok=False),
        ],
        accepted=False,
    )


def _install_fakes(monkeypatch, rule_id: str, gate_ok: bool, extra=None) -> FakeProvider:
    """Spec card / conformance review / drafter text, with an optional hook."""
    def reply(model, structured, tools):
        if extra is not None:
            override = extra(model, structured, tools)
            if override is not None:
                return override
        if structured is SpecCard:
            return _spec_card(rule_id)
        if structured is ConformanceReview:
            return ConformanceReview(ok=True, issues=[])
        return DRAFT_REPLY

    provider = install(reply)
    monkeypatch.setattr(fixtures_facade, "ensure_fixtures",
                        lambda card, base_models: _manifest(rule_id))
    monkeypatch.setattr(
        fixtures_facade, "run_acceptance_gate",
        lambda rid, code, manifest, real, timeout: _gate_report(rule_id, gate_ok),
    )
    monkeypatch.setattr(
        "bnbc.agent.execution.run_code_on_files", lambda code, files, t: []
    )
    return provider


@pytest.fixture
def rules_dir(tmp_path):
    """Input side: the rule definition only."""
    rdir = tmp_path / "rules"
    (rdir / "R.1").mkdir(parents=True)
    (rdir / "R.1" / "rule.json").write_text(
        json.dumps({
            "rule_id": "R.1", "title": "fake",
            "source_clauses": [], "statement": "some rule text",
            "scope_note": "", "references": {}, "terms": [],
        }),
        encoding="utf-8",
    )
    return rdir


@pytest.fixture
def artifacts_dir(tmp_path):
    """Output side: spec cards, checkers, evidence. Separate on purpose."""
    return tmp_path / "artifacts"


@pytest.fixture
def base_model(tmp_path):
    path = tmp_path / "base.ifc"
    path.write_text("dummy")  # never opened: gate + fixtures are faked
    return path


class TestGraphAcceptPath:
    def test_rule_accepted_and_stored(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        _install_fakes(monkeypatch, "R.1", gate_ok=True)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        rule_dir = artifacts_dir / "R.1"
        assert (rule_dir / "checker.py").exists()
        assert (rule_dir / "spec_card.yaml").exists()
        acceptance = json.loads((rule_dir / "acceptance.json").read_text(encoding="utf-8"))
        assert acceptance["accepted"] is True
        provenance = json.loads((rule_dir / "provenance.json").read_text(encoding="utf-8"))
        assert provenance["candidate_id"] == cfg.DRAFTER_MODEL
        assert provenance["llm_provider"] == cfg.LLM_PROVIDER
        assert provenance["agent_turns"] >= 1
        assert provenance["spec_revisions"] == 0
        assert provenance["token_usage"]["total"] > 0
        assert "total_cost_usd" in provenance["token_usage"]

    def test_tool_call_evaluate_path_accepts(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        """The drafter's own `evaluate` tool call ends the session."""
        calls = {"n": 0}

        def extra(model, structured, tools):
            if structured is not None:
                return None
            calls["n"] += 1
            return [ToolCall(id="call_1", name="evaluate",
                             args={"code": f"{GOOD_CODE}{PREREQS}"})]

        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)
        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        assert calls["n"] == 1  # one tool turn was enough
        assert state["candidate"]["agent_turns"] == 1

    def test_no_hitl_model_resolved_conventions_do_not_block(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        from bnbc.contracts import MeasurementConvention

        card = _spec_card("R.1")
        card.conventions = [MeasurementConvention(
            topic="datum", decision="conservative reading", status="proposed"
        )]
        _install_fakes(monkeypatch, "R.1", gate_ok=True,
                       extra=lambda m, s, t: card if s is SpecCard else None)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"  # proposed conventions never halt
        assert state["adjudication_status"] == "model_resolved"

    def test_chatter_reply_is_recovered_inside_the_session(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        """A reply without code is not terminal: the session nudges the model
        back into the protocol and the rule still lands."""
        turns = {"n": 0}

        def extra(model, structured, tools):
            if structured is not None:
                return None
            turns["n"] += 1
            return "Sorry, here is a sketch of an approach..." if turns["n"] == 1 else DRAFT_REPLY

        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)
        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        assert turns["n"] == 2  # chatter turn + code turn, one session


class TestConformanceIsAdvisoryOnly:
    def test_gate_accepted_candidate_stores_despite_blockers(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        """The deterministic gate is the authority; reviewer blockers on a
        gate-accepted candidate become provenance flags, never a veto."""
        monkeypatch.setattr(cfg, "ENABLE_CONFORMANCE_REVIEW", True)
        from bnbc.agent.conformance import ConformanceIssue

        review = ConformanceReview(ok=False, issues=[
            ConformanceIssue(condition="c1", severity="blocker", note="suspicious")
        ])
        _install_fakes(monkeypatch, "R.1", gate_ok=True,
                       extra=lambda m, s, t: review if s is ConformanceReview else None)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        provenance = json.loads(
            (artifacts_dir / "R.1" / "provenance.json").read_text(encoding="utf-8")
        )
        assert len(provenance["conformance"]["unresolved_blockers"]) == 1

    def test_reviewer_failure_never_blocks_storage(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        monkeypatch.setattr(cfg, "ENABLE_CONFORMANCE_REVIEW", True)
        _install_fakes(
            monkeypatch, "R.1", gate_ok=True,
            extra=lambda m, s, t: (
                RuntimeError("reviewer provider down") if s is ConformanceReview else None
            ),
        )

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"  # advisory failure is not fatal
        provenance = json.loads(
            (artifacts_dir / "R.1" / "provenance.json").read_text(encoding="utf-8")
        )
        assert provenance["conformance"]["ok"] is None


class TestPartialEvidence:
    def test_partial_evidence_written_on_rejection(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        """One hard condition must not erase the evidence for provable ones:
        rejection stores the candidate + per-condition verification under
        partial/ — without touching accepted=True semantics."""
        _install_fakes(monkeypatch, "R.1", gate_ok=False)
        monkeypatch.setattr(cfg, "AGENT_MAX_TURNS", 2)

        def mixed_gate(rid, code, manifest, real, timeout):
            return AcceptanceReport(rule_id="R.1", outcomes=[
                FixtureOutcome(fixture_id="R.1::F00", expected_verdict=Verdict.FAIL,
                               actual_verdict=Verdict.FAIL, conditions_hit=["c1"], ok=True),
                FixtureOutcome(fixture_id="R.1::F01", expected_verdict=Verdict.FAIL,
                               actual_verdict=Verdict.PASS, conditions_missed=["c2"], ok=False),
            ], accepted=False)

        monkeypatch.setattr(fixtures_facade, "run_acceptance_gate", mixed_gate)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        pdir = artifacts_dir / "R.1" / "partial"
        assert (pdir / "checker.py").exists()
        pa = json.loads((pdir / "partial_acceptance.json").read_text(encoding="utf-8"))
        assert pa["verified_conditions"] == ["c1"]
        rejection = json.loads(
            (artifacts_dir / "R.1" / "rejection.json").read_text(encoding="utf-8")
        )
        assert rejection["verified_conditions"] == ["c1"]
        # Partial evidence never enters the accepted index.
        from bnbc.agent.rule_store import RuleStore
        assert RuleStore(artifacts_dir).load_accepted() == []


class TestGraphRejectPath:
    def test_gate_failure_ends_in_best_draft_handoff(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        """When the session cannot satisfy the gate within its budget, the run
        terminates as a best-draft human handoff, never a crash."""
        _install_fakes(monkeypatch, "R.1", gate_ok=False)
        monkeypatch.setattr(cfg, "AGENT_MAX_TURNS", 2)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        rejection = json.loads(
            (artifacts_dir / "R.1" / "rejection.json").read_text(encoding="utf-8")
        )
        assert rejection["needs_human_intervention"] is True
        assert rejection["candidate"]["code"]
        assert (artifacts_dir / "R.1" / "partial" / "checker.draft.py").exists()

    def test_oracle_fault_triggers_one_spec_revision(self, monkeypatch, rules_dir, artifacts_dir, base_model):
        """Uniform not_applicable across scored fixtures routes to spec_revise
        (once), not to further drafting that provably cannot progress."""
        from bnbc.agent.spec_card import SpecRevision

        monkeypatch.setattr(cfg, "AGENT_MAX_TURNS", 1)
        spec_calls = {"n": 0}

        def extra(model, structured, tools):
            if structured is SpecCard:
                spec_calls["n"] += 1
                return _spec_card("R.1")
            if structured is SpecRevision:
                spec_calls["n"] += 1
                return SpecRevision(revision_note="weakened convention")
            return None

        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)
        monkeypatch.setattr(
            fixtures_facade, "run_acceptance_gate",
            lambda rid, code, manifest, real, timeout: _gate_report_na("R.1"),
        )

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        assert state["spec_revisions"] == 1        # outer loop used exactly once
        assert spec_calls["n"] == 2                # generate + revise, never more
        rejection = json.loads(
            (artifacts_dir / "R.1" / "rejection.json").read_text(encoding="utf-8")
        )
        assert rejection["spec_revisions"] == 1
        assert rejection["candidate"]["fault_class"] == "oracle"

    def test_plan_defects_trigger_sketch_level_repair(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        """Structured plan defects repair ONLY the defective sketches (one
        SketchRepairs call), not the whole card — then the plan passes and the
        rule lands."""
        from bnbc.agent.spec_card import SketchRepair, SketchRepairs
        from bnbc.fixtures.errors import PlanningError

        calls = {"plan": 0, "repair": 0}
        fixed_sketch = _spec_card("R.1").fixture_sketches[0]

        def extra(model, structured, tools):
            if structured is SketchRepairs:
                calls["repair"] += 1
                return SketchRepairs(repairs=[SketchRepair(index=0, sketch=fixed_sketch)])
            return None

        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)

        def plan_then_ok(card, base_models):
            calls["plan"] += 1
            if calls["plan"] == 1:
                raise PlanningError(
                    "fixture plan invalid:\n  - sketch #0 (c1): unknown operator",
                    defects=[{"sketch_index": 0,
                              "message": "sketch #0 (c1): unknown operator 'bogus'"}],
                )
            return _manifest("R.1")

        monkeypatch.setattr(fixtures_facade, "ensure_fixtures", plan_then_ok)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        assert calls["repair"] == 1          # one targeted repair call
        assert calls["plan"] == 2            # invalid, then valid
        assert state["spec_plan_regens"] == 1
        assert state["spec_card"]["version"] == 2  # spliced card, version bumped
        # A recovered plan failure must not leave a stale rejection reason.
        assert not state.get("escalation_reason")

    def test_planning_error_regenerates_spec_card_once_then_rejects(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        from bnbc.fixtures.errors import PlanningError

        monkeypatch.setattr(cfg, "MAX_PLAN_REGENS", 1)
        calls = {"plan": 0, "spec": 0}

        def extra(model, structured, tools):
            if structured is SpecCard:
                calls["spec"] += 1
                return _spec_card("R.1")
            return None

        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)

        def always_invalid(card, base_models):
            calls["plan"] += 1
            raise PlanningError("condition c1 needs >=2 violating sketches")

        monkeypatch.setattr(fixtures_facade, "ensure_fixtures", always_invalid)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        assert "fixture plan invalid" in state["escalation_reason"]
        assert calls["plan"] == 2   # initial + one retry (MAX_PLAN_REGENS=1)
        assert calls["spec"] == 2   # generate + regenerate
        assert state["spec_plan_regens"] == 1

    def test_llm_provider_failure_is_terminal_rejection(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        from bnbc.llm import LLMOutputError

        install(lambda model, structured, tools: LLMOutputError("output did not validate"))
        monkeypatch.setattr(fixtures_facade, "ensure_fixtures",
                            lambda card, base_models: _manifest("R.1"))

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        assert "LLM/provider failure" in state["escalation_reason"]


class TestSessionResilience:
    def test_mid_session_llm_failure_keeps_the_best_candidate(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        """The provider has already exhausted its keys and models by the time
        it raises. Losing an evaluated, gate-passing checker to one bad turn
        would be the expensive kind of wrong — the session ends with its best."""
        from bnbc.llm import LLMProviderError

        turns = {"n": 0}

        def extra(model, structured, tools):
            if structured is not None:
                return None
            turns["n"] += 1
            if turns["n"] == 1:
                return DRAFT_REPLY          # evaluated and gate-accepted
            return LLMProviderError("every key and model exhausted")

        # gate_ok=True, but the drafter keeps talking after its first module,
        # so a second turn happens and fails.
        _install_fakes(monkeypatch, "R.1", gate_ok=True, extra=extra)
        monkeypatch.setattr(cfg, "AGENT_MAX_TURNS", 4)

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "stored"
        assert (artifacts_dir / "R.1" / "checker.py").exists()

    def test_failure_before_any_candidate_still_rejects(
        self, monkeypatch, rules_dir, artifacts_dir, base_model
    ):
        """With nothing evaluated there is nothing to salvage — the failure
        must reach the graph guard rather than yield an empty candidate."""
        from bnbc.llm import LLMProviderError

        _install_fakes(
            monkeypatch, "R.1", gate_ok=True,
            extra=lambda m, s, t: (
                None if s is not None else LLMProviderError("provider down")
            ),
        )

        state = asyncio.run(run_rule("R.1", rules_dir=str(rules_dir),
                                     artifacts_dir=str(artifacts_dir),
                                     base_models=[str(base_model)]))
        assert state["status"] == "rejected"
        assert "LLM/provider failure" in state["escalation_reason"]
