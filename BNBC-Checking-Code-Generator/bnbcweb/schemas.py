"""Request/response models + checker catalogue discovery.

The catalogue is built by SCANNING the accepted-checker directories
(`rule-*/check_*.py`) — never by importing them. Metadata (rule id,
reference clause, title) is parsed out of the source text with regexes,
so listing the menu never executes checker code. Modules are imported
only inside the worker, when a check actually runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent

RULE_DIR_GLOB = "rule-*"
CHECKER_GLOB = "check_*.py"

_RULE_ID_RE = re.compile(r'^RULE_ID\s*=\s*"([^"]+)"', re.MULTILINE)
_RULE_REF_RE = re.compile(r'^RULE_REF\s*=\s*"([^"]+)"', re.MULTILINE)
# check_a1_egress_door_width.py -> "A1"; fallback when the module has no
# explicit RULE_ID constant (only rule-1 does).
_FILENAME_ID_RE = re.compile(r"check_([a-z]+\d+)_", re.IGNORECASE)
# rule-1.Egress_door_width_v1 -> "Egress door width"
_DIR_TITLE_RE = re.compile(r"^rule-\d+\.(.+?)_v\d+$")


class CheckerInfo(BaseModel):
    rule_id: str
    title: str
    domain: str          # architectural | structural
    reference: str       # the clause/standard the checker encodes
    checker_path: str    # repo-relative, for provenance in the report


class CheckRequest(BaseModel):
    rules: list[str]     # rule ids to run, e.g. ["A1", "S3"]


class CheckerResult(BaseModel):
    rule_id: str
    verdict: str                 # pass | fail | unknown | not_applicable | error
    violation_count: int | None = None
    summary: str | None = None
    duration_s: float
    error: str | None = None
    report: dict | None = None   # the checker's full result dict, verbatim


class JobStatus(BaseModel):
    job_id: str
    state: str
    filename: str
    created_at: str
    error: str | None = None
    results: list[CheckerResult] | None = None


def discover_checkers(repo_root: Path = REPO_ROOT) -> list[CheckerInfo]:
    """Scan rule-*/check_*.py and parse metadata from source text.

    Returns a stable, sorted catalogue. Unknown/undecorated checkers are
    still listed (with a filename-derived id) so nothing accepted is ever
    hidden from the menu."""
    out: list[CheckerInfo] = []
    for checker_path in sorted(repo_root.glob(f"{RULE_DIR_GLOB}/{CHECKER_GLOB}")):
        source = checker_path.read_text(encoding="utf-8", errors="replace")

        m = _RULE_ID_RE.search(source)
        if m:
            rule_id = m.group(1)
        else:
            m = _FILENAME_ID_RE.match(checker_path.name)
            rule_id = m.group(1).upper() if m else checker_path.stem

        m = _RULE_REF_RE.search(source)
        reference = m.group(1) if m else ""

        m = _DIR_TITLE_RE.match(checker_path.parent.name)
        title = m.group(1).replace("_", " ") if m else checker_path.stem

        domain = "architectural" if rule_id.upper().startswith("A") else "structural"

        out.append(CheckerInfo(
            rule_id=rule_id, title=title, domain=domain, reference=reference,
            checker_path=str(checker_path.relative_to(repo_root)).replace("\\", "/"),
        ))
    return out
