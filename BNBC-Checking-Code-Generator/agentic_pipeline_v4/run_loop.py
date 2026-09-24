"""
Driver for the bounded draft loop, judged by v4.

`run_pipeline.py` grades a checker once. This runs the loop: grade, hand the
structured feedback to a drafter, re-grade, up to `--max-rounds`, keeping the
best draft seen (the loop mechanics live in `agentic_pipeline_v3/loop.py`).

    python agentic_pipeline_v4/run_loop.py A6 path/to/check_a6.py --drafter llm
    python agentic_pipeline_v4/run_loop.py A6 path/to/check_a6.py --drafter manual

`--drafter llm` needs a configured provider (`GEMINI_API_KEYS` or
`OPENROUTER_API_KEY` in `.env`). `--drafter manual`, the default, prints the
feedback a drafter would receive and stops - useful for seeing exactly what
signal the harness produces before spending tokens on it.

Exit code 0 = accepted, 1 = rejected.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agentic_pipeline_v3"))

from harness import print_report  # noqa: E402  (v4's enriched reporter)
from loop import manual_stop_drafter, run_loop  # noqa: E402  (v3's loop mechanics)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("rule_id")
    parser.add_argument("checker_path", type=Path)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--drafter", choices=["manual", "llm"], default="manual")
    args = parser.parse_args()

    if not args.checker_path.exists():
        print(f"error: no checker at {args.checker_path}")
        return 2

    if args.drafter == "llm":
        from drafter import llm_drafter
        drafter = llm_drafter
    else:
        drafter = manual_stop_drafter

    report = run_loop(args.rule_id, args.checker_path,
                      max_rounds=args.max_rounds, drafter=drafter)
    if report is None:
        print("no report produced")
        return 1

    print_report(report)
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
