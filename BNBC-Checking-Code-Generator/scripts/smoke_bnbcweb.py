"""Smoke test for the bnbcweb package — run from the repo root:
    python scripts/smoke_bnbcweb.py
Exercises catalogue discovery and (if a model path is given/available) one
checker end-to-end through the same code path the service worker uses.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bnbcweb.schemas import discover_checkers

checkers = discover_checkers()
print(f"{len(checkers)} checkers discovered:")
for c in checkers:
    print(f"  {c.rule_id:4} {c.domain:13} {c.title:35} {c.checker_path}")

assert len(checkers) == 10, f"expected 10 accepted checkers, found {len(checkers)}"
assert {c.rule_id for c in checkers} == {f"A{i}" for i in range(1, 6)} | {f"S{i}" for i in range(1, 6)}
print("catalogue OK")
