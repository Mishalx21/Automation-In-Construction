"""
STEP 4 -- bounded, elitism-preserving draft loop.

Honesty note: this repo has no LLM API key configured (see the session's
own audit of .env.example), so there is no automated code-writing model to
plug in here right now. Rather than fake that capability, this module
implements the LOOP MECHANICS for real (bounded rounds, best-draft
retention, structured feedback handed to whatever drafts next) behind a
pluggable `drafter` callable. The default drafter is a manual stop: it
prints the mechanical feedback and returns None, meaning "a human (or a
future LLM call) must edit checker_path and the loop will pick up from
there." Wiring in bnbc/llm/ or any other provider later only requires
passing a different `drafter` function -- nothing else about the loop
changes, which is the whole point of keeping this pluggable instead of
hardcoding a fake success path.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from harness import RuleReport, run_rule

# A drafter takes (rule_id, checker_path, report, round_number) and either
# edits checker_path in place and returns True ("try again"), or returns
# False ("I have no further fix to propose, stop here").
Drafter = Callable[[str, Path, RuleReport, int], bool]


def manual_stop_drafter(rule_id: str, checker_path: Path, report: RuleReport, round_number: int) -> bool:
    print(f"\n--- round {round_number}: no automated drafter configured ---")
    print("FEEDBACK for a human (or a future LLM drafter) to act on:")
    print(report.feedback_text())
    print(f"Edit {checker_path} and re-run this loop to continue.\n")
    return False


def run_loop(
    rule_id: str,
    checker_path: Path,
    max_rounds: int = 5,
    drafter: Drafter = manual_stop_drafter,
) -> RuleReport:
    """Bounded loop: run -> if rejected, ask the drafter for a fix -> repeat.
    Never discards a completed round's result: if the loop exhausts its
    rounds or the drafter gives up, the BEST-scoring draft seen so far is
    restored to checker_path before returning (elitism -- see the earlier
    budget-exhaustion lesson: never end with less than your best attempt)."""
    best_report: Optional[RuleReport] = None
    best_score = -1
    best_snapshot: Optional[bytes] = None

    for round_number in range(1, max_rounds + 1):
        report = run_rule(rule_id, checker_path)
        score = sum(1 for r in report.testable if r.ok)

        if score > best_score:
            best_score = score
            best_report = report
            best_snapshot = checker_path.read_bytes()

        if report.accepted:
            return report

        keep_going = drafter(rule_id, checker_path, report, round_number)
        if not keep_going:
            break

    # restore the best draft seen, even if the final round regressed or the
    # drafter gave up mid-edit
    if best_snapshot is not None and checker_path.read_bytes() != best_snapshot:
        checker_path.write_bytes(best_snapshot)
    return best_report
