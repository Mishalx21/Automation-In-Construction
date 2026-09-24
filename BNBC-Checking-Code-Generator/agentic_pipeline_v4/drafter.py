"""
The drafter the v3 loop was built with a hole for.

`agentic_pipeline_v3/loop.py` implements the loop mechanics - bounded rounds,
elitism, structured feedback - behind a pluggable `drafter` callable, and its
docstring is explicit that no automated code-writing model was wired in
because the repo had no LLM configured. This module is that wiring, and
nothing else about the loop changes.

What the drafter is handed each round is the whole point: not "it failed", but
the harness's per-fixture verdict, WHICH fixture, what the fixture's own
mutation record says was changed, and - from v4 - the generalized failure
class when one of the three classifiers recognises the shape of the problem.
That is a far better prompt than the clause alone, and it is the reason the
loop converges instead of guessing.

Usage is through `run_loop.py`; this module only builds the prompt, calls the
model and writes the result.

It calls the repo's own provider-agnostic layer (`bnbc/llm/`), so it follows
whatever `LLM_PROVIDER` is configured and is metered and budget-capped like
every other model call in the project. With no API key set it raises a clear
error instead of silently doing nothing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bnbc import config as cfg  # noqa: E402
from bnbc.agent.ingest import load_rule  # noqa: E402
from bnbc.llm import call_llm  # noqa: E402
from bnbc.llm.base import system, user  # noqa: E402
from bnbc.llm.metering import TokenMeter  # noqa: E402
from bnbc.rulebook.render import render_context  # noqa: E402
from bnbc.rulebook.store import get_rulebook  # noqa: E402

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

SYSTEM_PROMPT = """\
You write Python checkers that test one BNBC clause against an IFC model.

Contract - the file you return must satisfy all of it:

* module-level `RULE_ID` and `RULE_REF` string constants, and one `COND_*`
  constant per condition the clause contains;
* `check_rule(model: ifcopenshell.file) -> dict` returning exactly these keys:
  `verdict`, `violations`, `violation_count`, `unknown_reasons`,
  `checked_summary`, `summary`;
* `verdict` is one of `pass`, `fail`, `unknown`, `not_applicable`. Return
  `unknown` when the model lacks the data to decide - never `pass`. Return
  `not_applicable` only when the clause genuinely does not govern this model;
* each violation is
  `{condition, description, rule_ref, threshold, locations: [{element, storey,
  measured}]}`, where `element` comes from `element_label()` so it carries the
  element's GlobalId, and `measured` is `key=value, key=value` pairs;
* a `main()` that takes one IFC path argument and prints the result as JSON;
* import shared helpers from `ifc_helpers.helpers` after inserting the repo
  root on `sys.path`, exactly as the reference checker does.

Write the module docstring as a statement of what you measure and which
conventions you adopted where the clause and the IFC schema do not line up.
Say what you do NOT check. A reviewer has to be able to argue with it.

Return one fenced Python block and nothing else.
"""


def _clause_context(rule_id: str) -> str:
    """The verbatim clauses this rule is made of, plus what they cite."""
    try:
        rule = load_rule(rule_id, cfg.RULES_DIR)
    except Exception as exc:  # noqa: BLE001
        return f"(no rules/{rule_id}/rule.json: {exc})"
    book = get_rulebook()
    context = book.resolve(
        rule.get("references") or {},
        clause_ids=rule.get("source_clauses") or [],
        term_ids=rule.get("terms") or [],
    )
    parts = [
        f"RULE {rule_id}: {rule.get('title', '')}",
        "",
        "STATEMENT (what must hold):",
        rule.get("statement", ""),
    ]
    if rule.get("scope_note"):
        parts += ["", "SCOPE NOTE (what is deliberately not checked):", rule["scope_note"]]
    parts += ["", "VERBATIM BNBC TEXT:", render_context(context)]
    if context.missing:
        parts += ["", f"(unresolved references: {context.missing})"]
    return "\n".join(parts)


def _feedback(report, round_number: int) -> str:
    """The harness's own words, plus v4's classification when it fired."""
    from harness import enrich  # v4's classifiers

    lines = [
        f"ROUND {round_number}: the harness REJECTED the current draft.",
        "",
        "Per-fixture result:",
    ]
    for result in report.results:
        mark = "FIXTURE-FAULT" if result.fixture_fault else ("ok" if result.ok else "FAIL")
        lines.append(
            f"  [{mark}] {result.case.category:8} {result.case.building:24} "
            f"verdict={result.verdict} reachable={result.reachable} "
            f"attribution={result.attribution_method}({result.attribution_ok})"
        )
    lines += ["", "Why the failures failed:", report.feedback_text()]

    try:
        classified = enrich(report)
    except Exception:  # noqa: BLE001
        classified = {}
    if classified:
        lines += ["", "Generalized failure class (v4):"]
        for fixture, notes in classified.items():
            for note in notes:
                lines.append(f"  - {fixture}: {note}")
    return "\n".join(lines)


def _extract_code(reply: str) -> str | None:
    match = _FENCE.search(reply or "")
    code = match.group(1) if match else (reply or "")
    code = code.strip()
    if "def check_rule" not in code:
        return None
    return code + "\n"


async def _ask(rule_id: str, current_source: str, feedback: str, meter: TokenMeter) -> str:
    messages = [
        system(SYSTEM_PROMPT),
        user(
            f"{_clause_context(rule_id)}\n\n"
            f"=== THE CHECKER AS IT STANDS ===\n```python\n{current_source}\n```\n\n"
            f"=== WHAT THE FIXTURE HARNESS SAYS ===\n{feedback}\n\n"
            "Rewrite the checker so every fixture passes. Fix the cause the harness "
            "names; do not merely widen a threshold until the fixtures go green, and "
            "do not special-case a fixture by GlobalId or by file name."
        ),
    ]
    return await call_llm(
        cfg.DRAFTER_MODEL, messages, meter, node="drafter",
        temperature=0.0, reasoning_effort=cfg.DRAFTER_REASONING_EFFORT,
    )


def llm_drafter(rule_id: str, checker_path: Path, report, round_number: int) -> bool:
    """Ask the configured model for a fix; write it and tell the loop to retry."""
    meter = TokenMeter()
    current = checker_path.read_text(encoding="utf-8") if checker_path.exists() else ""
    feedback = _feedback(report, round_number)

    print(f"\n--- round {round_number}: asking {cfg.DRAFTER_MODEL} for a fix ---")
    try:
        reply = asyncio.run(_ask(rule_id, current, feedback, meter))
    except Exception as exc:  # noqa: BLE001
        print(f"drafter call failed: {exc!r}")
        return False

    code = _extract_code(reply if isinstance(reply, str) else str(reply))
    if code is None:
        print("drafter returned no usable check_rule(); stopping.")
        return False

    checker_path.write_text(code, encoding="utf-8")
    usage = getattr(meter, "total", None)
    print(f"round {round_number}: wrote {len(code.splitlines())} lines to {checker_path.name}"
          + (f" ({usage} tokens)" if usage else ""))
    return True
