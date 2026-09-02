"""The drafting session: one tool-using LLM conversation per rule.

Design record: docs/AGENTIC-LOOP-DESIGN.md. A single bounded session replaces
the older blind-draft + whole-module-repair stages: the drafter inspects real
IFC content, writes a module, reads the fixture scorecard, and iterates —
which is how a competent human writes a checker. The tools it may call live
in :mod:`bnbc.agent.tools`.

Every bound is local and env-tunable: AGENT_MAX_TURNS, AGENT_MAX_INSPECTS,
AGENT_WALL_TIMEOUT_SECONDS, AGENT_CONTEXT_TOKENS, plus the shared
TOKEN_BUDGET_PER_RULE and the per-call wall timeout inherited from call_llm.
The session never grants acceptance: it returns its best candidate and the
graph's ``execute`` node re-runs that candidate from scratch as the sole
authority.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bnbc import config as cfg
from bnbc.llm import (
    BudgetExceeded,
    LLMError,
    Message,
    TokenMeter,
    ToolCall,
    call_llm,
    system,
    tool_result,
    user,
)
from bnbc.agent import tools as agent_tools
from bnbc.agent.execution import best_of, candidate_score
from bnbc.agent.prompts import (
    DRAFTER_SYSTEM_PROMPT,
    extract_code,
    render_exemplars,
    render_spec_card_compact,
)

logger = logging.getLogger("bnbc.agent.draft")

AGENT_PROTOCOL = """
You work in a tool loop until the checker passes every fixture:

0. CITATIONS: the BNBC source below already includes the clauses, tables,
   equations and figures this rule cites. If anything you read refers to
   something you cannot see — "Table 6.8.1", "Sec 8.3.5.4", "Eq. 6.8.6", a
   figure — call `fetch_context` with that reference. NEVER reconstruct a
   table's values, an equation, or a clause's wording from memory: if
   `fetch_context` says it is not in the rulebook, treat the datum as
   undeterminable and report `unknown` rather than guessing.
1. INSPECT: for any fixture whose scene/geometry you are not 100% sure
   about, `inspect` it (bar placements are WORLD-frame via ObjectPlacement;
   directrix coordinates are LOCAL). Also inspect a real base model when you
   rely on naming/pset assumptions.
2. Write the COMPLETE module and `evaluate` it.
3. Read the scorecard: fix exactly what it names (a fixture's "measured
   ground truth" is authoritative — it was verified independently). Keep
   what already passes working. Re-`evaluate` after each edit.
4. Before you consider the work done, `run_on_real` on one real model; if
   the result looks absurd (everything violating / everything skipped),
   investigate with `inspect` on that model — real exports use different
   representation idioms (IFC2X3 composite curves, mapped bodies, meters).
5. Stop when `evaluate` says accepted=true. Do not stop before that unless
   you are truly stuck; there is no fixed round limit, but every call costs
   budget, so be surgical.

The fixture list below tells you each fixture's file, expected verdict, and
what was done to build it — this is manufactured ground truth, use it.
"""


def _fixture_briefing(state: dict) -> str:
    """One line per fixture: id, expected verdict, how it was built."""
    manifest = state.get("fixture_manifest") or {}
    lines = []
    for spec in manifest.get("fixtures") or []:
        ops = " + ".join(s.get("operator", "?") for s in spec.get("steps") or []) \
            or spec.get("operator", "?")
        measured = ((spec.get("self_verification") or {}).get("measured") or "")[:180]
        lines.append(
            f"- {spec.get('fixture_id')} [{spec.get('expected_verdict')}] "
            f"file={spec.get('file_name')} built by: {ops}"
            + (f" | measured: {measured}" if measured else "")
        )
    return "\n".join(lines)


def _opening_brief(state: dict) -> str:
    """Everything the drafter is grounded in before its first move."""
    exemplars = render_exemplars(state.get("exemplars") or [])
    sections = [
        render_spec_card_compact(state.get("spec_card") or {}),
        # The spec card is an interpretation; this is what the code actually
        # says. A threshold the card paraphrased wrongly is visible here.
        ("## BNBC source (verbatim — the spec card above interprets this)\n"
         + state["rule_context"]) if state.get("rule_context") else "",
        "## Real-corpus profile\n" + state.get("model_profile", ""),
        "## ifc_helpers reference\n" + state.get("helper_reference", ""),
        ("## Exemplars (previously gate-verified checkers)\n" + exemplars)
        if exemplars else "",
        "## Fixtures (manufactured ground truth — every one must pass)\n"
        + _fixture_briefing(state),
        "Begin. Inspect what you need, then draft and evaluate.",
    ]
    return "\n\n".join(s for s in sections if s.strip())


def _context_chars(messages: list[Message]) -> int:
    return sum(len(m.content or "") for m in messages)


async def agentic_draft_node(state: dict) -> dict:
    """One bounded tool-using drafter session; returns its best candidate."""
    rule_id = state["rule_id"]
    meter = TokenMeter.from_state(state.get("token_usage"))
    model_name = cfg.DRAFTER_MODEL
    specs = agent_tools.tool_specs()
    ctx = agent_tools.ToolContext(state=state)

    messages: list[Message] = [
        system(DRAFTER_SYSTEM_PROMPT + "\n" + AGENT_PROTOCOL),
        user(_opening_brief(state)),
    ]

    best: dict[str, Any] = {}
    accepted = False
    turns = 0
    started = time.perf_counter()

    while turns < cfg.AGENT_MAX_TURNS:
        turns += 1
        if time.perf_counter() - started > cfg.AGENT_WALL_TIMEOUT_SECONDS:
            logger.warning("Rule %s: agent session wall timeout after %d turns", rule_id, turns)
            break
        if _context_chars(messages) / 4 > cfg.AGENT_CONTEXT_TOKENS:
            logger.warning("Rule %s: agent context ceiling reached — force-finishing", rule_id)
            break

        try:
            reply = await call_llm(
                model_name, messages, meter, "agent_draft",
                temperature=0.0, reasoning_effort=cfg.DRAFTER_REASONING_EFFORT,
                tools=specs,
            )
        except BudgetExceeded:
            raise  # the budget is spent — nothing here can recover it
        except LLMError as exc:
            # The provider already exhausted its own keys and models. Ending
            # the session with the best candidate so far beats discarding a
            # nearly-passing checker over one bad turn; if there is nothing
            # yet, the graph rejects with this reason either way.
            if not best.get("code") and not ctx.last_evaluation.get("code"):
                raise
            logger.warning(
                "Rule %s: LLM failure on turn %d (%s) — ending the session with "
                "the best candidate so far", rule_id, turns, exc,
            )
            break
        messages.append(reply.as_message())

        if not reply.tool_calls:
            # A textual reply: either a finished module (score it on the
            # drafter's behalf rather than discard a complete draft over a
            # protocol slip) or chatter — nudge back into the protocol.
            code, _errors = extract_code(reply.text)
            if code and code != ctx.latest_code:
                scorecard = agent_tools.dispatch(
                    ctx, ToolCall(id="auto_evaluate", name="evaluate", args={"code": code})
                )
                best = best_of(ctx.last_evaluation, best)
                accepted = bool(ctx.last_evaluation.get("accepted"))
                messages.append(user(
                    "I ran evaluate() on the module you just wrote:\n" + scorecard
                    + ("\nAccepted — you are done." if accepted
                       else "\nNot accepted yet — continue fixing via the tools.")
                ))
            elif not accepted:
                messages.append(user(
                    "Use the tools: `evaluate` your complete module, or "
                    "`inspect` a fixture. Reply with tool calls."
                ))
            if accepted:
                break
            continue

        for call in reply.tool_calls:
            result = agent_tools.dispatch(ctx, call)
            if call.name == "evaluate":
                best = best_of(ctx.last_evaluation, best)
                accepted = bool(ctx.last_evaluation.get("accepted"))
            messages.append(tool_result(call, result))

        if accepted:
            # End deterministically — the in-loop gate said yes; the graph's
            # execute node is the official authority anyway.
            break

    chosen = best if best.get("code") else ctx.last_evaluation
    candidate: dict[str, Any] = {
        "candidate_id": model_name,
        "model_name": model_name,
        "code": chosen.get("code", ""),
        "extraction_errors": (
            [] if chosen.get("code") else ["agent session produced no evaluated module"]
        ),
        "agent_turns": turns,
        "agent_inspects": ctx.inspects_used,
    }
    logger.info(
        "Rule %s: agent session ended after %d turn(s), %d inspect(s) — best %s, "
        "accepted(in-loop)=%s",
        rule_id, turns, ctx.inspects_used, candidate_score(chosen), accepted,
    )
    return {
        "candidate": candidate,
        "token_usage": meter.to_state(),
        "status": "drafted",
    }
