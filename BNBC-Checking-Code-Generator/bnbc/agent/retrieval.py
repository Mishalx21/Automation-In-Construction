"""Grounding material for the drafter: helper reference, exemplars, corpus
profile.

* :func:`helper_reference` renders the live ``ifc_helpers`` API from
  signatures and docstrings — a generated reference cannot drift from the
  code the way a hand-written cookbook did.
* :func:`retrieve_exemplars` indexes previously *accepted* rules from the
  filesystem store, plus curated seeds for cold start. Scoring is lexical
  (token-set Jaccard over spec-card condition texts).
* :func:`build_model_profile` summarises what the real IFC corpus actually
  contains, so the drafter grounds its data access in the models rather than
  in assumptions.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bnbc import config as cfg

if TYPE_CHECKING:  # import cycle: rule_store imports nothing from here
    from bnbc.agent.rule_store import RuleStore

logger = logging.getLogger("bnbc.agent.retrieval")

#: At most ONE exemplar, and only when genuinely similar: an irrelevant
#: exemplar (observed live: a hook-geometry checker retrieved for a
#: stirrup/splice rule from a library of one) adds up to 8 KB of misleading
#: code to every draft.
TOP_K_EXEMPLARS = int(os.environ.get("TOP_K_EXEMPLARS", "1"))
EXEMPLAR_MIN_SCORE = float(os.environ.get("EXEMPLAR_MIN_SCORE", "0.15"))
MAX_DOC_CHARS = 1200  # per-helper docstring budget in the reference

_WORD_RE = re.compile(r"[a-z0-9_]+")

_STOPWORDS = frozenset(
    "the a an of to in for and or with must shall be is are on at by from "
    "not no this that all any each".split()
)


# ---------------------------------------------------------------------------
# Helper reference (auto-generated from ifc_helpers docstrings)
# ---------------------------------------------------------------------------

def helper_reference() -> str:
    """Render the ifc_helpers API doc from live signatures + docstrings."""
    try:
        import ifc_helpers
    except Exception as exc:  # pragma: no cover — env without ifcopenshell
        logger.warning("ifc_helpers unavailable, helper reference empty: %s", exc)
        return "(ifc_helpers unavailable: %s)" % exc

    names = list(getattr(ifc_helpers, "__all__", [])) or [
        n for n in dir(ifc_helpers) if not n.startswith("_")
    ]
    lines: list[str] = [
        "# ifc_helpers reference (auto-generated from docstrings)",
        "# TOTALITY GUARANTEE: every function below is total — on missing or",
        "# malformed data it returns None/empty/default and NEVER raises.",
        "# Do NOT wrap ifc_helpers calls in try/except; check the return",
        "# value for None instead.",
    ]
    for name in names:
        fn = getattr(ifc_helpers, name, None)
        if not callable(fn):
            continue
        try:
            sig = str(inspect.signature(fn))
        except (TypeError, ValueError):
            sig = "(...)"
        # The full docstring matters: the return-shape sections are what stop
        # drafters from re-deriving parsers the library already provides
        # (observed live: first-paragraph-only reference -> hand-rolled,
        # broken directrix parsing in every draft).
        doc = (inspect.getdoc(fn) or "").strip()
        if len(doc) > MAX_DOC_CHARS:
            doc = doc[:MAX_DOC_CHARS] + " ..."
        indented = "\n".join(f"    {line}" for line in doc.splitlines())
        lines.append(f"- {name}{sig}:\n{indented}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lexical scoring
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS}


def condition_text(spec_card: dict[str, Any]) -> str:
    """Concatenated condition texts of a spec card dict (the retrieval key)."""
    parts: list[str] = [spec_card.get("rule_title", "")]
    for cond in spec_card.get("conditions", []) or []:
        parts.append(cond.get("id", ""))
        parts.append(cond.get("requirement", ""))
        parts.append(cond.get("applicability", ""))
        parts.extend(cond.get("required_data", []) or [])
    return " ".join(p for p in parts if p)


def lexical_score(query: str, doc: str) -> float:
    """Token-set Jaccard similarity in [0, 1]."""
    q, d = _tokenize(query), _tokenize(doc)
    if not q or not d:
        return 0.0
    return len(q & d) / len(q | d)


# ---------------------------------------------------------------------------
# Exemplar retrieval
# ---------------------------------------------------------------------------

def _spec_summary(spec_card: dict[str, Any] | None, rule_id: str) -> str:
    if not spec_card:
        return f"rule {rule_id} (no spec card on disk)"
    conds = spec_card.get("conditions", []) or []
    lines = [f"rule {rule_id}: {spec_card.get('rule_title', '')}".strip()]
    for cond in conds[:4]:
        lines.append(f"  - {cond.get('id', '?')}: {cond.get('requirement', '')}")
    return "\n".join(lines)


def retrieve_exemplars(
    spec_card: dict[str, Any],
    store: RuleStore,
    k: int = TOP_K_EXEMPLARS,
) -> list[dict[str, Any]]:
    """Top-k previously ACCEPTED checkers most similar to ``spec_card``.

    Returns ``[{rule_id, score, spec_summary, code}]``. Scoring is lexical
    Jaccard over condition texts — with an exemplar library in the tens,
    embeddings buy nothing a token-set overlap does not already give. The
    rule never retrieves itself.
    """
    query = condition_text(spec_card)
    rule_id = spec_card.get("rule_id", "")
    scored: list[tuple[float, dict[str, Any]]] = []

    for entry in store.load_accepted():
        if entry["rule_id"] == rule_id:
            continue
        doc = condition_text(entry.get("spec_card") or {}) or entry.get("code", "")
        score = lexical_score(query, doc)
        scored.append((score, {
            "rule_id": entry["rule_id"],
            "score": round(score, 4),
            "spec_summary": _spec_summary(entry.get("spec_card"), entry["rule_id"]),
            "code": entry.get("code", ""),
        }))

    scored.sort(key=lambda t: (-t[0], t[1]["rule_id"]))
    return [item for score, item in scored[:k] if score >= EXEMPLAR_MIN_SCORE]


# ---------------------------------------------------------------------------
# Base-model profile (drafter grounding)
# ---------------------------------------------------------------------------

_IFC_CLASS_RE = re.compile(r"\bIfc[A-Z][A-Za-z]+\b")

#: Structural classes always profiled when present, on top of whatever the
#: spec card mentions.
_DEFAULT_PROFILE_CLASSES = (
    "IfcBeam", "IfcColumn", "IfcWall", "IfcSlab", "IfcReinforcingBar",
)


def _spec_ifc_classes(spec_card: dict[str, Any]) -> list[str]:
    """Every IFC class name the spec card mentions anywhere."""
    import json as _json

    text = _json.dumps(spec_card, default=str)
    return sorted(set(_IFC_CLASS_RE.findall(text)))


def build_model_profile(base_models: list[str], spec_card: dict[str, Any]) -> str:
    """Compact real-corpus profile for the draft prompt.

    The drafter is otherwise blind to actual model content — the profile is
    the mechanised version of "write a scratch script and look at the data
    first" (the Code-Agent skill's single most effective mandate). Class
    counts come from the plan-time census; one sample element per relevant
    class contributes its set attributes and pset names/keys.
    """
    try:
        from bnbc.fixtures.census import load_census, load_digest  # lazy: ifcopenshell
    except Exception as exc:  # pragma: no cover — env without ifcopenshell
        logger.warning("census unavailable, model profile empty: %s", exc)
        return ""

    spec_classes = _spec_ifc_classes(spec_card)
    wanted = set(spec_classes) | set(_DEFAULT_PROFILE_CLASSES)
    lines: list[str] = []
    for path_str in (base_models or [])[:3]:
        path = Path(path_str)
        census = load_census(path)
        if not census:
            continue
        present = sorted(c for c in wanted if c in census)
        counts = ", ".join(f"{c} x{census[c].get('count', 0)}" for c in present) \
            or "(none of the rule's classes present)"
        lines.append(f"- {path.name}: {counts}")
        # Digest budget: the rule's own classes first, generic defaults after.
        prioritised = [c for c in spec_classes if c in census] + [
            c for c in present if c not in spec_classes
        ]
        digest = load_digest(path, prioritised[:5]) or {}
        scale = digest.get("unit_scale_mm")
        if scale:
            lines.append(f"  length unit: {scale:g} mm per model unit")
        for cls, info in (digest.get("classes") or {}).items():
            attrs = info.get("sample_attributes") or {}
            attr_text = ", ".join(f"{k}={v!r}" for k, v in list(attrs.items())[:10])
            lines.append(f"  sample {cls}: {attr_text[:400]}")
            psets = info.get("sample_psets") or {}
            for pname, keys in list(psets.items())[:6]:
                lines.append(f"    pset {pname}: {', '.join(keys[:12])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def retrieve_node(state: dict) -> dict:
    """Gather exemplars, the helper reference, and the base-model profile."""
    from bnbc.agent.rule_store import RuleStore  # local import: avoid cycle

    store = RuleStore(Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR))
    spec_card = state.get("spec_card") or {}
    exemplars = retrieve_exemplars(spec_card, store)
    logger.info(
        "Rule %s: retrieved %d exemplar(s): %s",
        state.get("rule_id"), len(exemplars), [e["rule_id"] for e in exemplars],
    )
    profile = build_model_profile(list(state.get("base_models") or []), spec_card)
    return {
        "exemplars": exemplars,
        "helper_reference": helper_reference(),
        "model_profile": profile,
        "status": "retrieved",
    }
