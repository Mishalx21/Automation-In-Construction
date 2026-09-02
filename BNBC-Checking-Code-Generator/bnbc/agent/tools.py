"""Tools the drafter may call during its session.

One registry, one dispatch path: a tool is an :class:`AgentTool` — the schema
the model sees plus the handler that runs it — and adding a capability means
appending one entry to :data:`AGENT_TOOLS`. Nothing else in the agent knows
which tools exist.

The tools here port the capabilities an IDE coding agent gets for free (see
docs/AGENTIC-LOOP-DESIGN.md) as *bounded* calls, plus one for the regulation
itself:

* ``inspect``       — read-only Python probe against one IFC file, in a
                      sandboxed subprocess with a stdout cap;
* ``evaluate``      — the static gate + fixture gate, returning the full
                      per-fixture scorecard with each fixture's independently
                      measured ground truth;
* ``fetch_context`` — resolve a BNBC reference (table, figure, equation,
                      clause) out of the rulebook;
* ``run_on_real``   — advisory run on one unlabeled production model.

Showing the drafter the fixture expectations is legitimate: FIV fixtures are
manufactured, self-verified ground truth, so matching them IS implementing
the spec card. Acceptance authority stays with the deterministic gate — the
graph's ``execute`` node re-runs the final candidate from scratch.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bnbc import config as cfg
from bnbc.contracts import FixtureManifest
from bnbc.llm import ToolCall, ToolSpec
from bnbc.rulebook import find_references
from bnbc.agent import fixtures_facade
from bnbc.agent.execution import parse_ast_limitations

logger = logging.getLogger("bnbc.agent.tools")


# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Mutable per-session state the handlers read and write."""

    state: dict
    inspects_used: int = 0
    #: Source of the most recently evaluated module (what ``run_on_real`` runs).
    latest_code: str = ""
    #: Candidate fields produced by the most recent ``evaluate`` call.
    last_evaluation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTool:
    spec: ToolSpec
    run: Callable[[ToolContext, dict[str, Any]], str]


# ---------------------------------------------------------------------------
# inspect — sandboxed read-only probe
# ---------------------------------------------------------------------------

_INSPECT_BANNED = ("os", "subprocess", "shutil", "socket", "pathlib")
_INSPECT_TIMEOUT_S = 30


def inspect_snippet_errors(code: str) -> list[str]:
    """Static safety for probe snippets: read-only, no process/file access."""
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except Exception as exc:
        return [f"snippet does not parse: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _INSPECT_BANNED:
                    errors.append(f"forbidden import '{alias.name}'")
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _INSPECT_BANNED:
                errors.append(f"forbidden import from '{node.module}'")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("open", "exec", "eval", "__import__"):
            errors.append(f"forbidden call '{node.func.id}()' — probes are read-only")
    return errors


def _resolve_target(state: dict, target: str) -> Path | None:
    """fixture id / fixture file name / real model name -> absolute path."""
    manifest = state.get("fixture_manifest") or {}
    fixture_dir = cfg.FIXTURES_DIR / state.get("rule_id", "")
    for spec in manifest.get("fixtures") or []:
        if target in (spec.get("fixture_id"), spec.get("file_name")):
            return fixture_dir / spec.get("file_name")
    for base in state.get("base_models") or []:
        if Path(base).name == target or target in Path(base).name:
            return Path(base)
    candidate = fixture_dir / target
    return candidate if candidate.exists() else None


def _run_inspect(ctx: ToolContext, args: dict[str, Any]) -> str:
    ctx.inspects_used += 1
    if ctx.inspects_used > cfg.AGENT_MAX_INSPECTS:
        return (
            f"inspect budget exhausted ({cfg.AGENT_MAX_INSPECTS}) — "
            "work from the evaluate scorecard now"
        )

    target = str(args.get("target", ""))
    code = str(args.get("code", ""))
    path = _resolve_target(ctx.state, target)
    if path is None or not path.exists():
        known = [
            f.get("fixture_id")
            for f in (ctx.state.get("fixture_manifest") or {}).get("fixtures", [])
        ]
        reals = [Path(b).name for b in ctx.state.get("base_models") or []]
        return f"unknown target {target!r}. Known fixtures: {known}; real models: {reals}"

    errors = inspect_snippet_errors(code)
    if errors:
        return "snippet rejected: " + "; ".join(errors)

    with tempfile.TemporaryDirectory(prefix="bnbc_inspect_") as tmp:
        snippet_path = Path(tmp) / "probe.py"
        snippet_path.write_text(code, encoding="utf-8")
        wrapper = f"""\
import sys, ifcopenshell
sys.path.insert(0, {str(cfg.IFC_HELPERS_DIR.parent)!r})
import ifc_helpers  # noqa: F401 — available to the probe
model = ifcopenshell.open({str(path)!r})
exec(compile(open({str(snippet_path)!r}, encoding="utf-8").read(), "probe.py", "exec"))
"""
        try:
            proc = subprocess.run(
                [sys.executable, "-c", wrapper],
                capture_output=True, text=True, timeout=_INSPECT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"probe timed out after {_INSPECT_TIMEOUT_S}s — inspect less at once"

    out = (proc.stdout or "").strip()
    cap = cfg.AGENT_INSPECT_STDOUT_CHARS
    text = out[:cap] + (" ...[truncated]" if len(out) > cap else "")
    if proc.returncode != 0:
        text += "\nPROBE ERROR: " + (proc.stderr or "").strip()[-1000:]
    return text or "(no output — print() what you want to see)"


# ---------------------------------------------------------------------------
# evaluate — static gate + fixture gate scorecard
# ---------------------------------------------------------------------------

def score_candidate(state: dict, code: str) -> tuple[dict[str, Any], str]:
    """Run the static + fixture gates; return (candidate fields, scorecard).

    The scorecard is what the drafter reads: per fixture, what was expected,
    what the checker returned, which conditions/elements it missed, what it
    claimed it saw, and the fixture's independently measured ground truth.
    """
    rule_id = state["rule_id"]
    manifest = FixtureManifest.model_validate(state.get("fixture_manifest") or {})
    fixtures_by_id = {f.fixture_id: f for f in manifest.fixtures}

    static_errors = parse_ast_limitations(code)
    if static_errors:
        fields = {
            "code": code, "static_errors": static_errors, "schema_errors": [],
            "accepted": False, "kill_rate": 0.0, "acceptance": {"outcomes": []},
        }
        return fields, (
            "STATIC GATE REJECTED the module (fix these before anything else):\n- "
            + "\n- ".join(static_errors)
        )

    report = fixtures_facade.run_acceptance_gate(
        rule_id, code, manifest, [], cfg.CHECKER_TIMEOUT_SECONDS
    )
    rows = []
    for outcome in report.outcomes:
        spec = fixtures_by_id.get(outcome.fixture_id)
        row: dict[str, Any] = {
            "fixture": outcome.fixture_id,
            "expected": outcome.expected_verdict.value,
            "actual": outcome.actual_verdict.value if outcome.actual_verdict else "error",
            "ok": outcome.ok,
        }
        if not outcome.ok:
            if outcome.conditions_missed:
                row["conditions_missed"] = outcome.conditions_missed
            if outcome.elements_missed:
                row["violation_must_name_guids"] = outcome.elements_missed
            if outcome.checker_summary:
                row["your_checker_reported"] = outcome.checker_summary[:300]
            if spec is not None and spec.self_verification is not None:
                row["fixture_measured_truth"] = spec.self_verification.measured[:400]
            if outcome.execution_error:
                row["error"] = outcome.execution_error[-1500:]
        rows.append(row)

    ok_n = sum(1 for o in report.outcomes if o.ok)
    fields = {
        "code": code,
        "static_errors": [],
        "schema_errors": list(report.schema_errors),
        "accepted": report.accepted,
        "kill_rate": report.kill_rate,
        "runtime_ms_max": report.runtime_ms_max,
        "acceptance": report.model_dump(mode="json"),
    }
    scorecard = json.dumps({
        "accepted": report.accepted,
        "fixtures_ok": f"{ok_n}/{len(report.outcomes)}",
        "kill_rate": round(report.kill_rate, 2),
        "schema_errors": [str(e)[:300] for e in report.schema_errors][:5],
        "results": rows,
    }, indent=1, ensure_ascii=False)
    return fields, scorecard


def _run_evaluate(ctx: ToolContext, args: dict[str, Any]) -> str:
    fields, scorecard = score_candidate(ctx.state, str(args.get("code", "")))
    ctx.last_evaluation = fields
    ctx.latest_code = fields.get("code", "")
    return scorecard


# ---------------------------------------------------------------------------
# run_on_real — advisory sanity check
# ---------------------------------------------------------------------------

def _run_on_real(ctx: ToolContext, args: dict[str, Any]) -> str:
    from bnbc.agent.execution import run_code_on_files

    model_name = str(args.get("model_name", ""))
    matches = [
        Path(b) for b in ctx.state.get("base_models") or [] if model_name in Path(b).name
    ]
    if not matches:
        available = [Path(b).name for b in ctx.state.get("base_models") or []]
        return f"unknown real model {model_name!r}; available: {available}"
    if not ctx.latest_code:
        return "no evaluated code yet — call evaluate first"

    result = run_code_on_files(
        ctx.latest_code, matches[:1], cfg.CHECKER_TIMEOUT_SECONDS
    )[0]
    if not result.get("success"):
        return f"CRASHED on real model: {str(result.get('error'))[-1500:]}"
    payload = result.get("result") or {}
    return json.dumps({
        "model": matches[0].name,
        "verdict": payload.get("verdict"),
        "violation_count": payload.get("violation_count"),
        "summary": str(payload.get("summary", ""))[:400],
        "note": "advisory only — no expected answer; judge plausibility",
    }, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------------------
# fetch_context — look up BNBC content the brief did not already carry
# ---------------------------------------------------------------------------

def _run_fetch_context(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Resolve one BNBC reference out of the rulebook.

    Ingest already inlines everything a rule *directly* cites, so this covers
    the transitive hop: 8.3.7.2 cites Sec 8.3.5.4, which in turn carries
    Eq. 6.8.6. Answering "not in the rulebook" is a correct answer — far
    better than the model reciting a half-remembered table.
    """
    from bnbc.rulebook import get_rulebook, render_reference

    ref = str(args.get("reference", "")).strip()
    if not ref:
        return "reference is required, e.g. 'Table 6.8.1', 'Sec 8.3.5.4', 'Eq. 6.8.6'"

    rulebook = get_rulebook()
    found = find_references(ref)
    # A bare id ("6.8.1", "8.1.9.4") carries no kind — try every reader.
    if not found:
        found = {"tables": [ref], "figures": [ref], "equations": [ref], "clauses": [ref]}

    rendered: list[str] = []
    for kind, values in found.items():
        for value in values:
            if kind == "clauses":
                for clause in rulebook.resolve_clause_ref(value):
                    rendered.append(render_reference("clause", clause))
            else:
                payload = getattr(rulebook, kind[:-1])(value)
                if payload is not None:
                    rendered.append(render_reference(kind, payload))
    if not rendered:
        return (
            f"{ref!r} is not in the rulebook. Do NOT invent its content — say "
            "what you cannot determine and let the verdict be unknown."
        )
    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

AGENT_TOOLS: tuple[AgentTool, ...] = (
    AgentTool(
        spec=ToolSpec(
            name="inspect",
            description=(
                "Run a short read-only Python snippet against ONE IFC file to "
                "look at its actual content before/while coding. The variable "
                "`model` is a pre-opened ifcopenshell file; `ifc_helpers` and "
                "`ifcopenshell` are importable. print() what you want to see "
                "(coordinates, psets, directrix points, spacings...). No "
                "writes, no os/subprocess, output truncated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "fixture id (e.g. 'R.1::F03'), fixture file name, "
                            "or a real base-model file name"
                        ),
                    },
                    "code": {"type": "string", "description": "Python snippet using `model`"},
                },
                "required": ["target", "code"],
            },
        ),
        run=_run_inspect,
    ),
    AgentTool(
        spec=ToolSpec(
            name="evaluate",
            description=(
                "Run the COMPLETE checker module against every fixture and get "
                "the per-fixture scorecard (expected vs actual verdict, missed "
                "conditions/elements, what your checker reported, the fixture's "
                "independently measured ground truth, error details). Call this "
                "after every meaningful edit. Acceptance requires every fixture ok."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The complete Python module (check_rule + PREREQUISITES)",
                    }
                },
                "required": ["code"],
            },
        ),
        run=_run_evaluate,
    ),
    AgentTool(
        spec=ToolSpec(
            name="fetch_context",
            description=(
                "Look up BNBC content by reference — a table ('Table 6.8.1'), a "
                "figure ('Figure 6.8.2'), an equation ('Eq. 6.8.6'), or a clause "
                "or section ('Sec 8.3.5.4', '8.1.9.4'). Use it whenever the spec "
                "card or a clause cites something you were not given. If the "
                "reference is not in the rulebook you will be told so — never "
                "invent the content of a table or clause you cannot see."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "e.g. 'Table 6.8.1', 'Sec 8.3.5.4', 'Eq. 6.8.6'",
                    }
                },
                "required": ["reference"],
            },
        ),
        run=_run_fetch_context,
    ),
    AgentTool(
        spec=ToolSpec(
            name="run_on_real",
            description=(
                "Advisory sanity check: run your latest evaluated code on one "
                "UNLABELED real production model and see the verdict/summary. No "
                "expected answer exists — use it to catch absurdities (e.g. 100% "
                "of elements violating usually means a measurement or unit bug "
                "on real-world representation idioms)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "model_name": {"type": "string", "description": "real corpus file name"}
                },
                "required": ["model_name"],
            },
        ),
        run=_run_on_real,
    ),
)

_BY_NAME = {tool.spec.name: tool for tool in AGENT_TOOLS}


def tool_specs() -> list[ToolSpec]:
    """The schemas advertised to the model."""
    return [tool.spec for tool in AGENT_TOOLS]


def dispatch(ctx: ToolContext, call: ToolCall) -> str:
    """Execute one tool call; an unknown name is reported, never raised."""
    tool = _BY_NAME.get(call.name)
    if tool is None:
        return f"unknown tool {call.name!r}; available: {sorted(_BY_NAME)}"
    try:
        return tool.run(ctx, call.args or {})
    except Exception as exc:
        # A tool crash is feedback, not a run-ending failure: the drafter can
        # correct its arguments, and the session bounds still hold.
        logger.warning("Tool %s raised: %s", call.name, exc)
        return f"tool {call.name} failed: {exc}"
