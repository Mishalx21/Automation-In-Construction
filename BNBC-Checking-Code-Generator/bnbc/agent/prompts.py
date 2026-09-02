"""Drafter prompts and code extraction.

The system prompt below is the drafter's whole standing brief: the output
contract (CheckResultV2), the violation-attribution contract the gate
enforces, the static-gate rules, and the field doctrine distilled from
previously verified checkers. The rendering helpers turn the run's grounding
material — spec card, exemplars — into prompt text, and :func:`extract_code`
recovers a module from a free-text reply.

Nothing here is test-file metadata: the drafter never sees labels.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

from bnbc.contracts import SpecCard

logger = logging.getLogger("bnbc.agent.prompts")

EXEMPLAR_CODE_CAP = 8000  # chars of exemplar code per prompt slot

DRAFTER_SYSTEM_PROMPT = """\
You write production-quality Python rule checkers for BNBC building-code
compliance over IFC models (ifcopenshell). You are given a human-adjudicated
Spec Card — it is the authoritative interpretation of the rule. Implement
EVERY condition exactly as specified, including its conventions.

Measurement doctrine: GEOMETRY FIRST. Derive measurements primarily from
parsed geometry (directrix points, bend info, bounding boxes) using the
ifc_helpers geometry functions; consult property sets only as a fallback when
geometry is unavailable.

Deliverable: ONE complete Python module in a single ```python fenced block,
defining:

1. `check_rule(model)` — takes an open ifcopenshell model, returns a dict in
   the CheckResultV2 shape below.
2. `PREREQUISITES` — a module-level list of dicts, one per data prerequisite
   from the spec card: {"condition": str, "entity": str, "requirement": str,
   "on_missing": "unknown"|"not_applicable"} (the machine-readable
   data-prerequisites manifest for this checker).

CheckResultV2 output contract (ALL keys required):
{
  "verdict": "pass" | "fail" | "unknown" | "not_applicable",
  "violations": [{"condition": <spec condition id>, "description": str,
                  "rule_ref": str, "threshold": str,
                  "locations": [{"element": "Name (GlobalId)",
                                 "storey": str, "measured": str}]}],
  "violation_count": <int, MUST equal total number of locations>,
  "unknown_reasons": [{"condition": str, "missing": str,
                       "affected_elements": int}],
  "checked_summary": {<condition id>: {"elements_checked": int,
                                       "elements_skipped": int,
                                       "skip_reasons": {str: int}}},
  "summary": str
}

Four-valued verdict semantics (STRICT):
- "pass": every checked element complies AND at least one element was checked.
- "fail": at least one violation (violations list non-empty).
- "unknown": required data missing/unparseable for some condition —
  unknown_reasons must say what is missing.
- "not_applicable": no elements in scope for this rule in this model.
- NEVER return "pass" with zero elements checked. checked_summary is REQUIRED
  and must account for every condition.

Violation attribution contract (the acceptance gate enforces this — a correct
verdict with wrong attribution still FAILS the fixture):
- Attribute each violation to the specific NON-COMPLIANT elements themselves
  (e.g. the reinforcing bars whose spacing violates), NOT to their host or
  container (wall/slab/beam/storey). Put the host in "storey"/"description"
  if useful; the "element" field must be the offending element.
- The gate matches the GlobalIds of the elements it perturbed against your
  location strings — every location's "element" MUST embed the element's
  GlobalId in the "Name (GlobalId)" format.
- When a violation inherently involves several elements (a spacing pair, a
  lap-splice pair), report each involved element as its own location entry.

Constraints:
- Import helpers as `import ifc_helpers` (reference below). Do not re-derive
  what a helper provides. In particular: NEVER write your own directrix/
  geometry parser — `ifc_helpers.get_bar_bend_info(bar)` already returns the
  ordered segment list (see its reference entry for the exact dict shape);
  build your measurements on top of it.
- ifc_helpers functions are TOTAL: they return None/empty on missing or
  malformed data and never raise. Do NOT wrap them in try/except — check
  return values for None. The only call that may need a try/except is
  `ifcopenshell.geom.create_shape`, and its handler must record a
  skip_reason/unknown_reason (a bare `pass` body is rejected).
- Deterministic: no randomness, no network, no file writes.
- A deterministic AST gate REJECTS the module if it contains ANY of:
  * an `except` handler whose body is only `pass` — never swallow failures
    silently; record a skip_reason or unknown_reason instead;
  * a collection slice `[:N]` with N >= 10 — evaluate ALL elements, no
    sampling;
  * imports of os / subprocess / shutil.

Field-proven doctrine (distilled from 19 previously verified checkers):
- NEVER call `ifcopenshell.geom.create_shape` on IfcReinforcingBar — the
  mesh kernel times out on thousands-of-bars models. Bar geometry comes from
  the ifc_helpers fast path (directrix/bend-info/centroid/bbox). create_shape
  on hosts (beams/columns/walls/slabs) is fine; cache each host's shape.
- Normalise units ONCE with `ifc_helpers.length_unit_to_mm(model)` and guard
  against double scaling (a value that is already in mm must not be scaled
  again).
- Diameter: NominalDiameter is often absent on real models —
  `ifc_helpers.get_bar_diameter_mm` already falls back to parsing the Name
  (e.g. "Rebar Bar:10mm", "20M") and geometry; use it, never re-parse.
- Bar-to-host association: try relationships first
  (IfcRelContainedInSpatialStructure / IfcRelAggregates / IfcRelNests), then
  fall back to geometric OVERLAP (Z-range/bbox intersection with tolerance) —
  not strict containment; real bars poke out of their hosts.
- COORDINATE-FRAME TRAP: `get_bar_directrix_points` returns LOCAL coordinates;
  a bar's world position lives in its ObjectPlacement. NEVER compare directrix
  coordinates across bars — two identical bars offset by their placements have
  identical local directrixes. For cross-bar geometry (lap-splice overlap,
  spacing, position along the member) use the world-frame helpers:
  `get_bar_bbox_fast` / `get_bar_centroid_fast` / `get_bar_placement_fast`
  (placement-inclusive, mm). A lap splice is two parallel bars whose
  WORLD-frame axis intervals overlap.
- No magic constants fitted to particular files: no hardcoded size filters,
  severity ratios, or model-specific name tests. Tolerances are small
  geometric epsilons (e.g. 1e-6 relative, 0.5 mm absolute), stated once.
- Performance: precompute per-element data in one pass and reuse it; avoid
  O(n^2) scans over thousands of bars — group by host or spatial cell first.
"""


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def render_spec_card_compact(spec_card: dict[str, Any] | SpecCard) -> str:
    """Compact, token-frugal text rendering of a spec card."""
    card = spec_card if isinstance(spec_card, SpecCard) else SpecCard.model_validate(spec_card)
    lines = [f"SPEC CARD — rule {card.rule_id} (v{card.version}): {card.rule_title}"]
    if card.scope_note:
        lines.append(f"scope: {card.scope_note}")
    lines.append("conditions:")
    for c in card.conditions:
        lines.append(f"  [{c.id}] {c.requirement}")
        if c.applicability:
            lines.append(f"    applies to: {c.applicability}")
        for e in c.exceptions:
            lines.append(f"    except: {e}")
        if c.formula:
            lines.append(f"    formula: {c.formula}")
        for t in c.thresholds:
            lines.append(f"    threshold: {t.parameter} {t.operator} {t.value} {t.unit}".rstrip())
        if c.required_data:
            lines.append(f"    required data: {', '.join(c.required_data)}")
    if card.conventions:
        lines.append("adjudicated conventions (binding):")
        for conv in card.conventions:
            lines.append(f"  - {conv.topic}: {conv.decision}")
    if card.prerequisites:
        lines.append("data prerequisites:")
        for p in card.prerequisites:
            lines.append(
                f"  - [{p.condition}] {p.entity}: {p.requirement} (on missing -> {p.on_missing.value})"
            )
    return "\n".join(lines)


def render_exemplars(exemplars: list[dict[str, Any]]) -> str:
    """Previously gate-verified checkers, as prompt text (empty if none).

    Retrieval already applies a relevance floor, so whatever arrives here is
    worth its tokens; each exemplar's code is capped so one long checker
    cannot crowd out the rest of the brief.
    """
    parts: list[str] = []
    for i, ex in enumerate(exemplars or [], 1):
        code = ex.get("code", "")
        if len(code) > EXEMPLAR_CODE_CAP:
            code = code[:EXEMPLAR_CODE_CAP] + "\n# ... (truncated)"
        parts += [
            f"### Exemplar {i} — {ex.get('rule_id', '?')} (previously verified checker)",
            ex.get("spec_summary", ""),
            "```python",
            code,
            "```",
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

_PY_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_BLOCK_RE = re.compile(r"```[a-zA-Z0-9]*\s*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> tuple[str, list[str]]:
    """Extract checker code from an LLM reply.

    Collect all ```python blocks (fall back to any fenced block, then the raw
    text), try longest-first, and return the first candidate that
    ``ast.parse``s. Returns ``(code, errors)`` — code may be "" if nothing
    parses.
    """
    text = text or ""
    blocks = _PY_BLOCK_RE.findall(text) or _ANY_BLOCK_RE.findall(text)
    candidates = sorted((b.strip() for b in blocks), key=len, reverse=True)
    if not candidates and text.strip():
        candidates = [text.strip()]

    errors: list[str] = []
    for cand in candidates:
        try:
            ast.parse(cand)
            return cand, errors
        except SyntaxError as exc:
            errors.append(f"candidate block failed ast.parse: {exc}")
    errors.append("no parseable python code block found in the reply")
    return "", errors
