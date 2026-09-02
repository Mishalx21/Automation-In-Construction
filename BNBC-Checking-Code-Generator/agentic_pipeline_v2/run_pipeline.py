"""
Driver for the agentic checker-generation feedback loop.

Usage:
    python run_pipeline.py <RULE_ID> <path-to-candidate-checker.py>

Prints a per-fixture pass/fail table plus, on rejection, a structured
FEEDBACK block naming exactly which entity type/GUID was missed and why.
That feedback is meant to be read by the drafting agent (human or LLM) on
its next turn -- it is generated mechanically by harness.py, never guessed
by another model.

Exit code 0 = accepted, 1 = rejected.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import print_report, run_rule  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: python {Path(__file__).name} <RULE_ID> <path-to-checker.py>")
        return 2
    rule_id = sys.argv[1]
    checker_path = Path(sys.argv[2]).resolve()
    if not checker_path.exists():
        print(f"error: checker file not found: {checker_path}")
        return 2
    report = run_rule(rule_id, checker_path)
    print_report(report)
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
