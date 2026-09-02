"""EXTERNAL label-based evaluation of generated checkers — a DEV-ONLY script.

This exists to answer "did the agent actually get it right?" during
development. It is deliberately the only thing in the repository that touches
labels, and it holds none of them: both the labelled test cases and the IFC
files they name live in the **Code-Agent** repository, which is where they
were curated.

    Code-Agent/data-v2/
    ├── Rules/<rule_id>/rule.json   test_cases: [{file_name, expected_result}]
    └── IFC-files/*.ifc             the models those cases name

Nothing under ``bnbc/`` may import this module or read those paths: the
agent's core claim is that it produces correct checkers WITHOUT labelled test
cases, and keeping the labels in another repository is what makes that claim
structural rather than a promise.

Usage:
    python scripts/evaluate_v2.py 8.1.2.1.A [8.1.2.1.B ...]
    python scripts/evaluate_v2.py --all
    python scripts/evaluate_v2.py --all --code-agent D:/Code-Agent

The Code-Agent checkout defaults to a sibling directory (``../Code-Agent``)
and can be overridden with ``--code-agent`` or ``CODE_AGENT_ROOT``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
DEFAULT_CODE_AGENT = REPO_ROOT.parent / "Code-Agent"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("evaluate")


class Labels:
    """Read-only view of the Code-Agent label set."""

    def __init__(self, code_agent_root: Path):
        self.root = code_agent_root / "data-v2"
        self.rules_dir = self.root / "Rules"
        self.ifc_dir = self.root / "IFC-files"

    def check(self) -> str | None:
        """A one-line reason the label set is unusable, or None."""
        for path, what in ((self.rules_dir, "labelled rules"), (self.ifc_dir, "IFC corpus")):
            if not path.exists():
                return f"{what} not found at {path}"
        return None

    def test_cases(self, rule_id: str) -> list[dict[str, Any]] | None:
        path = self.rules_dir / rule_id / "rule.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get("test_cases") or None


def discover_rules() -> list[str]:
    """Every rule that has a stored checker."""
    return sorted(p.parent.name for p in ARTIFACTS_DIR.glob("*/checker.py"))


def load_checker(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("checker", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate_rule(rule_id: str, labels: Labels) -> dict[str, Any]:
    checker_path = ARTIFACTS_DIR / rule_id / "checker.py"
    if not checker_path.exists():
        return {"status": "error", "message": f"no checker.py at {checker_path}"}

    test_cases = labels.test_cases(rule_id)
    if not test_cases:
        return {"status": "no_test_cases",
                "message": f"no labelled cases for {rule_id} in {labels.rules_dir}"}

    try:
        checker = load_checker(checker_path)
    except Exception as exc:
        return {"status": "error",
                "message": f"failed to load checker.py: {exc}\n{traceback.format_exc()}"}

    import ifcopenshell

    cases: list[dict[str, Any]] = []
    successes = 0

    for case in test_cases:
        file_name = case["file_name"]
        expected_compliant = case["expected_result"]
        # Files named for another rule are out of scope for this checker; the
        # only correct answers there are not_applicable/unknown.
        in_scope = file_name.startswith(rule_id)
        expected = ("pass" if expected_compliant else "fail") if in_scope else "N/A or unknown"

        ifc_path = labels.ifc_dir / file_name
        if not ifc_path.exists():
            cases.append({
                "file_name": file_name, "expected": expected, "actual": "missing_file",
                "status": "ERROR", "message": f"IFC file not found at {ifc_path}",
            })
            continue

        try:
            model = ifcopenshell.open(str(ifc_path))
            result = checker.check_rule(model)
            verdict = result.get("verdict")
            if in_scope:
                ok = verdict == ("pass" if expected_compliant else "fail")
            else:
                ok = verdict in ("not_applicable", "unknown")
            successes += int(ok)
            cases.append({
                "file_name": file_name, "expected": expected, "actual": verdict,
                "status": "PASS" if ok else "FAIL",
                "message": f"Verdict: {verdict}. Summary: {result.get('summary', '')}",
            })
        except Exception as exc:
            cases.append({
                "file_name": file_name, "expected": expected, "actual": "exception",
                "status": "ERROR",
                "message": f"execution crashed: {exc}\n{traceback.format_exc()}",
            })

    return {"status": "completed", "total": len(test_cases),
            "successes": successes, "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rules", nargs="*", help="rule ids to evaluate")
    parser.add_argument("--all", action="store_true", help="every rule with a checker.py")
    parser.add_argument(
        "--code-agent",
        default=os.environ.get("CODE_AGENT_ROOT", str(DEFAULT_CODE_AGENT)),
        help="path to the Code-Agent checkout holding data-v2/",
    )
    args = parser.parse_args()

    rule_ids = discover_rules() if args.all else args.rules
    if not rule_ids:
        parser.print_help()
        return 2

    labels = Labels(Path(args.code_agent))
    problem = labels.check()
    if problem:
        logger.error("%s (--code-agent / CODE_AGENT_ROOT)", problem)
        return 1
    print(f"labels + corpus: {labels.root}")

    overall_ok = True
    for rule_id in rule_ids:
        print(f"\n{'=' * 75}\n  Evaluating rule: {rule_id}\n{'=' * 75}")
        result = evaluate_rule(rule_id, labels)

        if result["status"] == "error":
            print(f"Error: {result['message']}")
            overall_ok = False
            continue
        if result["status"] == "no_test_cases":
            print(f"Skipped: {result['message']}")
            continue

        print(f"{result['total']} labelled case(s).\n")
        print(f"{'Test File':<25} | {'Expected':<15} | {'Actual':<10} | {'Status':<8}")
        print("-" * 75)
        for case in result["cases"]:
            print(f"{case['file_name']:<25} | {case['expected']:<15} | "
                  f"{case['actual']:<10} | {case['status']:<8}")
            if case["status"] != "PASS":
                print(f"  -> {case['message']}")
        print("-" * 75)
        print(f"Summary: {result['successes']}/{result['total']} cases passed.")

        if result["successes"] != result["total"]:
            overall_ok = False

    print(f"\n{'=' * 75}\n  FINAL: {'SUCCESS' if overall_ok else 'FAILURE'}\n{'=' * 75}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
