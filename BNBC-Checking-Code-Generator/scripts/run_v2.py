"""Generate rule checkers with the agent — the primary entry point.

Usage:
    python scripts/run_v2.py 8.1.2.1.A [8.1.2.1.B ...]
    python scripts/run_v2.py --all          # every rules/<id>/ with a rule.json

Everything is read from and written to the repository: inputs live in
``rules/<id>/rule.json``, and each rule ends either ``stored`` (accepted
checker + provenance under ``rules/<id>/``) or ``rejected`` (structured
reason in ``rules/<id>/rejection.json``). The run continues to the next rule
either way — there is no human gate.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bnbc import config as cfg  # noqa: E402
from bnbc.agent.graph import run_rule  # noqa: E402
from bnbc.llm import LLMError, get_provider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def discover_rules() -> list[str]:
    return sorted(p.parent.name for p in cfg.RULES_DIR.glob("*/rule.json"))


def summarize(rule_id: str, state: dict) -> str:
    usage = state.get("token_usage") or {}
    status = state.get("status", "?")
    line = (
        f"{rule_id}: {status}  "
        f"({usage.get('total', 0):,} tokens, ${usage.get('total_cost_usd', 0.0):.4f})"
    )
    if status != "stored":
        line += f"\n    reason: {state.get('escalation_reason', 'see rejection.json')}"
    return line


async def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    rule_ids = discover_rules() if args == ["--all"] else args

    # Preflight: validate the provider before any work. Fixture materialisation
    # takes minutes, and discovering a missing key or SDK afterwards wastes it.
    try:
        get_provider().preflight()
    except LLMError as exc:
        logging.error("LLM provider unavailable: %s", exc)
        return 2

    print(f"provider: {cfg.LLM_PROVIDER} | spec: {cfg.SPEC_MODEL_NAME} | "
          f"drafter: {cfg.DRAFTER_MODEL}")

    results: dict[str, dict] = {}
    for rule_id in rule_ids:
        print(f"\n{'=' * 60}\n  {rule_id}\n{'=' * 60}")
        try:
            results[rule_id] = await run_rule(rule_id)
        except Exception as exc:  # invocation errors (missing rule.json etc.)
            logging.error("Rule %s: invocation failed: %s", rule_id, exc)
            results[rule_id] = {"status": "invocation_error", "escalation_reason": str(exc)}

    print(f"\n{'=' * 60}\n  SUMMARY\n{'=' * 60}")
    for rule_id, state in results.items():
        print(summarize(rule_id, state))
    stored = sum(1 for s in results.values() if s.get("status") == "stored")
    print(f"\n{stored}/{len(results)} rule(s) accepted.")
    return 0 if stored == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
