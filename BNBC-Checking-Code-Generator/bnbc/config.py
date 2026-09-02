"""Configuration for the BNBC checking-code generator.

Single source of truth for paths, model selection, and every loop/budget
bound. The pipeline is filesystem-only: inputs and outputs live under the
repository, there is no service dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_ROOT: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = APP_ROOT.parent

load_dotenv(PROJECT_ROOT / ".env")

# Inputs — never written by a run.
RULEBOOK_DIR: Path = PROJECT_ROOT / "rulebook"      # verbatim BNBC (built once)
RULES_DIR: Path = PROJECT_ROOT / "rules"            # the checkable rule definitions
IFC_DIR: Path = PROJECT_ROOT / "models"             # real IFC corpus
#: Per-model token prices, kept beside the metering module that reads them.
MODEL_PRICES_FILE: Path = APP_ROOT / "llm" / "model_prices.json"

# Outputs — everything a run produces, regenerable.
ARTIFACTS_DIR: Path = PROJECT_ROOT / "artifacts"    # per-rule spec card + checker + evidence
FIXTURES_DIR: Path = ARTIFACTS_DIR / "fixtures"     # generated fixtures + manifests

# Runtime library the GENERATED checkers import (`import ifc_helpers`); it sits
# at the repository root because it is their public contract, not agent internals.
IFC_HELPERS_DIR: Path = PROJECT_ROOT / "ifc_helpers"

# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------
#: "gemini" | "openrouter" — see bnbc/llm/registry.py. Adding a backend means
#: one LLMProvider subclass plus an entry in _MODEL_DEFAULTS below.
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()

# -- Gemini ---------------------------------------------------------------
# GEMINI_API_KEYS accepts a pool: "k1,k2,k3", '["k1","k2"]', or a single key.
# The provider rotates keys on quota/rate limits and marks invalid keys dead.
#
# Every model here is used: quota is per model, so a rate-limited one is parked
# and the next takes over. Adding a model adds its quota to the pool. The
# default chain is the text models measured to handle native structured output,
# tool calling, and the SpecCard schema at flash latency. Deliberately absent:
# the *-pro models (capable but >75s on a trivial call, which would blow the
# drafter session's wall clock) and the *-lite tiers (they pass the capability
# probes, but "as good as 2.5-flash at writing a checker" is unmeasured).
GEMINI_MODEL_FALLBACKS: list[str] = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL_FALLBACKS",
        "gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash",
    ).split(",")
    if m.strip()
]

#: How long a rate-limited or overloaded Gemini model is parked before it is
#: tried again. Quota is per model, so parking one and moving to the next is
#: what turns several models' separate limits into one throughput budget.
GEMINI_MODEL_COOLDOWN_SECONDS: float = float(
    os.environ.get("GEMINI_MODEL_COOLDOWN_SECONDS", "60")
)

# -- OpenRouter -----------------------------------------------------------
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
#: Optional provider pin for OpenRouter routing (fallbacks stay enabled).
OPENROUTER_PROVIDER: str = os.environ.get("OPENROUTER_PROVIDER", "")

# ---------------------------------------------------------------------------
# Model selection (per provider defaults; every entry is env-overridable)
# ---------------------------------------------------------------------------
_MODEL_DEFAULTS: dict[str, dict[str, str]] = {
    "gemini": {
        # Verified against the live API for the two things that actually break
        # models here: native structured output over SpecCard (which carries
        # free-form dict fields) and tool calling.
        "spec": "gemini-3.6-flash",
        "drafter": "gemini-3.6-flash",
        # A reviewer must be a different model from the drafter, or it agrees
        # with itself.
        "reviewer": "gemini-2.5-flash",
    },
    "openrouter": {
        # deepseek-v4-pro is the default: glm-5.2's structured+reasoning calls
        # burned the full completion budget with zero content, twice, and its
        # drafts were the only unparseable ones observed live.
        "spec": "deepseek/deepseek-v4-pro",
        "drafter": "deepseek/deepseek-v4-pro",
        "reviewer": "google/gemini-2.5-flash",
    },
}


def _model(role: str, env_var: str) -> str:
    defaults = _MODEL_DEFAULTS.get(LLM_PROVIDER, _MODEL_DEFAULTS["gemini"])
    return os.environ.get(env_var, "").strip() or defaults[role]


#: Strongest model; one call per rule, produces the Spec Card.
SPEC_MODEL_NAME: str = _model("spec", "SPEC_MODEL")
#: The single drafter that runs the agentic tool session.
DRAFTER_MODEL: str = _model("drafter", "DRAFTER_MODEL")
#: Advisory conformance reviewer (never an acceptance authority).
REVIEWER_MODEL_NAME: str = _model("reviewer", "REVIEWER_MODEL")

# ---------------------------------------------------------------------------
# LLM call bounds
# ---------------------------------------------------------------------------
#: Reasoning-effort cap for draft calls ("" = provider default). Uncapped
#: reasoning drafts were observed at ~15 min per call.
DRAFTER_REASONING_EFFORT: str = os.environ.get("DRAFTER_REASONING_EFFORT", "medium")
#: Spec-stage reasoning is minimised by default: effort levels proved
#: advisory — a drafter burned 18-22K reasoning tokens into the completion cap
#: on spec calls at both "medium" and "low". Spec outputs are structured data,
#: not proofs; the whole completion budget must go to the output.
SPEC_REASONING_EFFORT: str = os.environ.get("SPEC_REASONING_EFFORT", "none")
#: Per-socket-read timeout. NOTE: this only fires when the connection goes
#: fully silent — it does NOT bound a slow generation.
LLM_REQUEST_TIMEOUT_SECONDS: int = int(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "600"))
#: True wall-clock ceiling per LLM call. Observed live: a provider keeps the
#: connection warm while the upstream model generates, so a reasoning runaway
#: sailed 28 minutes past the read timeout above.
LLM_WALL_TIMEOUT_SECONDS: int = int(os.environ.get("LLM_WALL_TIMEOUT_SECONDS", "900"))
#: Completion-token cap per call; without it a runaway burns the provider
#: default (tens of thousands of tokens) before failing.
LLM_MAX_COMPLETION_TOKENS: int = int(os.environ.get("LLM_MAX_COMPLETION_TOKENS", "32768"))

# ---------------------------------------------------------------------------
# Execution settings
# ---------------------------------------------------------------------------
CHECKER_TIMEOUT_SECONDS: int = int(os.environ.get("CHECKER_TIMEOUT_SECONDS", "120"))
#: Concurrent checker subprocesses (execute node + acceptance gate).
EXEC_MAX_WORKERS: int = int(os.environ.get("EXEC_MAX_WORKERS", "4"))

# ---------------------------------------------------------------------------
# Pipeline bounds — see ARCHITECTURE.md
# ---------------------------------------------------------------------------
#: Advisory conformance review after gate acceptance. Default OFF: it cannot
#: veto or route, and live runs showed it mostly inflating style notes.
ENABLE_CONFORMANCE_REVIEW: bool = (
    os.environ.get("ENABLE_CONFORMANCE_REVIEW", "false").lower() == "true"
)
#: Agentic draft loop (AGENTIC-LOOP-DESIGN.md): one tool-using drafter session
#: replaces blind draft + whole-module repair. All bounds are local.
#: LLM turns in one session (a turn = one model reply, with or without tools).
AGENT_MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "24"))
#: Read-only inspection probes per session (each is a sandboxed subprocess).
AGENT_MAX_INSPECTS: int = int(os.environ.get("AGENT_MAX_INSPECTS", "10"))
#: Wall-clock ceiling for the whole session.
AGENT_WALL_TIMEOUT_SECONDS: int = int(os.environ.get("AGENT_WALL_TIMEOUT_SECONDS", "1800"))
#: Rough context ceiling (chars/4 heuristic) — past it the session force-ends
#: with the best candidate so the conversation can never grow unboundedly.
AGENT_CONTEXT_TOKENS: int = int(os.environ.get("AGENT_CONTEXT_TOKENS", "96000"))
#: Per-probe stdout cap in the agent's inspect feedback.
AGENT_INSPECT_STDOUT_CHARS: int = int(os.environ.get("AGENT_INSPECT_STDOUT_CHARS", "4000"))
#: Outer-loop bound: spec-card revisions triggered by an oracle-fault signature
#: at the gate (systematic verdict-class mismatch the code cannot fix).
MAX_SPEC_REVISIONS: int = int(os.environ.get("MAX_SPEC_REVISIONS", "1"))
#: Plan-time loop bound: spec-card regenerations triggered by planner/build
#: defects (invalid sketches, unsatisfiable targets, degenerate params).
MAX_PLAN_REGENS: int = int(os.environ.get("MAX_PLAN_REGENS", "2"))
#: Oracle-fault signature threshold: fraction of scored (fail/pass-expected)
#: fixtures that must miss with one uniform wrong verdict class to blame the
#: spec/fixture contract instead of the code.
ORACLE_FAULT_MISS_RATIO: float = float(os.environ.get("ORACLE_FAULT_MISS_RATIO", "0.9"))
#: Hard per-rule token ceiling and soft alert threshold.
TOKEN_BUDGET_PER_RULE: int = int(os.environ.get("TOKEN_BUDGET_PER_RULE", "500000"))
TOKEN_ALERT_PER_RULE: int = int(os.environ.get("TOKEN_ALERT_PER_RULE", "300000"))

# ---------------------------------------------------------------------------
# LangSmith tracing (optional)
# ---------------------------------------------------------------------------
LANGSMITH_TRACING: bool = os.environ.get("LANGSMITH_TRACING", "false").lower() == "true"
LANGSMITH_API_KEY: str = os.environ.get("LANGSMITH_API_KEY", "")
LANGSMITH_PROJECT: str = os.environ.get("LANGSMITH_PROJECT", "bnbc-rule-checker")
LANGSMITH_ENDPOINT: str = os.environ.get(
    "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
)

# The tracer reads the LANGCHAIN_* names; export them so LangGraph node traces
# and the explicitly traced LLM calls (bnbc/llm/tracing.py) land in one project.
if LANGSMITH_TRACING:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_TRACING"] = "true"
    if LANGSMITH_API_KEY:
        os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY
    if LANGSMITH_PROJECT:
        os.environ["LANGCHAIN_PROJECT"] = LANGSMITH_PROJECT
    if LANGSMITH_ENDPOINT:
        os.environ["LANGCHAIN_ENDPOINT"] = LANGSMITH_ENDPOINT
