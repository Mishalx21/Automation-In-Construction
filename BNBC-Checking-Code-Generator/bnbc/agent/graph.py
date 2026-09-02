"""The agent graph — one tool-using drafter session, one deterministic
oracle; every node bounded, every path terminal.

    ingest -> spec_card -> fixture_plan -> retrieve -> agentic_draft
        -> execute -> route:
              accepted    -> conformance (advisory, off the critical path)
                             -> store -> END
              oracle fault-> spec_revise -> fixture_plan (bounded outer loop:
                             MAX_SPEC_REVISIONS; fresh agent session)
              else        -> reject -> END (best draft + needs_human handoff)

    fixture_plan may also loop back to spec_card on an invalid plan, bounded
    by MAX_PLAN_REGENS.

    agentic_draft (docs/AGENTIC-LOOP-DESIGN.md) runs an internal bounded
    tool-call session (inspect / evaluate / run_on_real) and returns its
    best candidate; the execute node stays the SOLE acceptance authority
    by re-running that candidate from scratch.

Failure policy: per-fixture build/self-verification failures map back to
their source sketches and route through the bounded plan-regen loop
(fixtures_facade); BudgetExceeded and residual FixtureError (infrastructure:
missing base file, etc.) are converted by the node guard into a terminal
``rejected`` status with the full reason — the graph can neither loop
unboundedly nor wait on a human.
The conformance reviewer is advisory-only and handles its own failures: a
reviewer error must never cost us a gate-accepted checker.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from langgraph.graph import END, StateGraph

from bnbc import config as cfg
from bnbc.llm import BudgetExceeded, is_llm_infra_error
from bnbc.agent.conformance import conformance_node
from bnbc.agent.execution import execute_node
from bnbc.agent.fixtures_facade import fixture_plan_node
from bnbc.agent.ingest import ingest_node
from bnbc.agent.rule_store import reject_node, store_node
from bnbc.agent.spec_card import spec_card_node, spec_revise_node
from bnbc.agent.state import AgentState, get_candidate

logger = logging.getLogger("bnbc.agent.graph")

NodeFn = Callable[[dict], Awaitable[dict]]


def _guarded(node_fn: NodeFn) -> NodeFn:
    """Convert budget/fixture/LLM-infra failures into a rejectable status.

    Anything else is a genuine bug and still crashes (the runner isolates it per
    rule) — masking programming errors as rejections would poison the data.
    Every failure return carries the best-known token accounting: call_llm
    attaches its meter snapshot to exceptions it raises (observed live: a
    $0.10 terminal rejection previously reported "0 tokens").
    """

    @wraps(node_fn)
    async def _wrapped(state: dict) -> dict:
        from bnbc.fixtures.errors import FixtureError  # lazy: ifcopenshell

        def _usage(exc: Exception) -> dict:
            return getattr(exc, "usage", None) or state.get("token_usage") or {}

        try:
            return await node_fn(state)
        except BudgetExceeded as exc:
            logger.error("%s: token budget exhausted: %s", node_fn.__name__, exc)
            return {
                "status": "failed",
                "escalation_reason": f"token budget exhausted in {node_fn.__name__}: {exc}",
                "token_usage": _usage(exc),
            }
        except FixtureError as exc:
            logger.error("%s: fixture engine failure: %s", node_fn.__name__, exc)
            return {
                "status": "failed",
                "escalation_reason": f"fixture engine failure in {node_fn.__name__}: {exc}",
                "token_usage": _usage(exc),
            }
        except Exception as exc:
            if not is_llm_infra_error(exc):
                raise
            logger.error("%s: LLM/provider failure: %s", node_fn.__name__, exc)
            return {
                "status": "failed",
                "escalation_reason": f"LLM/provider failure in {node_fn.__name__}: {exc}",
                "token_usage": _usage(exc),
            }

    return _wrapped


def _failed(state: dict) -> bool:
    return state.get("status") == "failed"


def route_after_node(state: dict) -> str:
    """Linear stages: continue unless the guard flagged a failure."""
    return "reject" if _failed(state) else "continue"


def route_after_fixture_plan(state: dict) -> str:
    """Invalid plan -> regenerate the spec card (bounded), then reject."""
    if _failed(state):
        return "reject"
    if state.get("status") == "plan_invalid":
        if int(state.get("spec_plan_regens") or 0) >= cfg.MAX_PLAN_REGENS:
            return "reject"  # the retry budget is spent
        return "spec_card"
    return "retrieve"


def route_after_execute(state: dict) -> str:
    """Accept | spec_revise | reject — the pipeline's only decision point.
    The deterministic gate is the sole acceptance authority.

    The agentic draft session has already iterated code-level fixes to its
    budget; what remains is either acceptance, an oracle-fault signature
    (systematic verdict-class mismatch — the spec/fixture contract is broken
    and no code edit can move the vector, so it gets one bounded spec
    revision + a fresh agent session), or the best-draft human handoff.
    """
    if _failed(state):
        return "reject"
    cand = get_candidate(state)
    if cand is None:
        return "reject"
    if cand.get("accepted"):
        return "conformance"
    if (
        cand.get("fault_class") == "oracle"
        and int(state.get("spec_revisions") or 0) < cfg.MAX_SPEC_REVISIONS
    ):
        return "spec_revise"
    return "reject"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("ingest", _guarded(ingest_node))
    graph.add_node("spec_card", _guarded(spec_card_node))
    graph.add_node("fixture_plan", _guarded(fixture_plan_node))
    graph.add_node("retrieve", _guarded(_retrieve))
    graph.add_node("agentic_draft", _guarded(_agentic_draft))
    graph.add_node("execute", _guarded(execute_node))
    graph.add_node("spec_revise", _guarded(spec_revise_node))
    # Advisory-only: handles its own failures so a reviewer error can never
    # reject a gate-accepted checker (hence not _guarded).
    graph.add_node("conformance", conformance_node)
    graph.add_node("store", _guarded(store_node))
    graph.add_node("reject", reject_node)

    graph.set_entry_point("ingest")
    for src, dst in (
        ("ingest", "spec_card"),
        ("spec_card", "fixture_plan"),
        ("retrieve", "agentic_draft"),
        ("agentic_draft", "execute"),
        ("spec_revise", "fixture_plan"),
    ):
        graph.add_conditional_edges(src, route_after_node, {"continue": dst, "reject": "reject"})
    graph.add_conditional_edges(
        "fixture_plan", route_after_fixture_plan,
        {"retrieve": "retrieve", "spec_card": "spec_card", "reject": "reject"},
    )
    graph.add_conditional_edges(
        "execute", route_after_execute,
        {"conformance": "conformance", "spec_revise": "spec_revise",
         "reject": "reject"},
    )
    graph.add_edge("conformance", "store")
    graph.add_edge("store", END)
    graph.add_edge("reject", END)
    return graph.compile()


async def _retrieve(state: dict) -> dict:
    from bnbc.agent.retrieval import retrieve_node  # lazy: imports ifc_helpers

    return await retrieve_node(state)


async def _agentic_draft(state: dict) -> dict:
    from bnbc.agent.agentic_draft import agentic_draft_node  # lazy: ifcopenshell chain

    return await agentic_draft_node(state)


async def run_rule(rule_id: str, **overrides: Any) -> dict:
    """Run one rule end-to-end; returns the final state."""
    compiled = build_graph()
    state: dict = {"rule_id": rule_id, **overrides}
    # Recursion bound: the linear stages plus MAX_PLAN_REGENS plan loops and
    # MAX_SPEC_REVISIONS outer loops stay well under 40 for any sane config;
    # 100 is a hard backstop.
    return await compiled.ainvoke(state, config={"recursion_limit": 100})
