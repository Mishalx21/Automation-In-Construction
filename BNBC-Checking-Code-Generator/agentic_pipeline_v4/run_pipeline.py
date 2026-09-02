"""
Driver for the v4 harness (v3's judge + classified diagnosis).

Usage:
    python run_pipeline.py <RULE_ID> <path-to-candidate-checker.py> [--no-multi]

Exit code 0 = accepted, 1 = rejected (identical semantics to v3 --
v4 changes diagnosis only, not the accept/reject decision).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import print_report, run_rule  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    include_multi = "--no-multi" not in sys.argv
    if len(args) != 2:
        print(f"Usage: python {Path(__file__).name} <RULE_ID> <path-to-checker.py> [--no-multi]")
        return 2
    rule_id, checker_path = args[0], Path(args[1]).resolve()
    if not checker_path.exists():
        print(f"error: checker file not found: {checker_path}")
        return 2
    report = run_rule(rule_id, checker_path, include_multi=include_multi)
    print_report(report)
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
