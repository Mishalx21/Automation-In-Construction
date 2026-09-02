"""Ingest node: load the rule definition, resolve its rulebook context, and
locate the model corpus.

This is the single seam through which regulatory text enters the agent.
``rules/<rule_id>/rule.json`` is a *definition*, not a document: it names the
BNBC clauses the rule checks, a curated statement of what must hold, and the
tables, figures, equations and clauses it cites. The verbatim source for all
of those lives in ``rulebook/`` and is resolved here, so every later stage
sees the regulation as the code actually writes it.

Two things are deliberately absent. There are no labelled test cases to
ignore — they live in another repository entirely, so "the agent never sees
labels" is a property of the layout rather than of this module's manners. And
nothing here invents content: an unresolvable citation is reported to the
drafter as unresolved, never filled in from the model's memory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bnbc import config as cfg
from bnbc.rulebook import Rulebook, render_context

logger = logging.getLogger("bnbc.agent.ingest")

RULE_FILENAME = "rule.json"


def load_rule(rule_id: str, rules_dir: Path | str) -> dict:
    """The rule definition. Hard-fails: a rule that cannot be read is an
    invocation error, not a pipeline verdict."""
    path = Path(rules_dir) / rule_id / RULE_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"rule {rule_id}: no {RULE_FILENAME} at {path}")
    rule = json.loads(path.read_text(encoding="utf-8"))
    if not str(rule.get("statement", "")).strip():
        raise ValueError(f"rule {rule_id}: empty statement in {path}")
    return rule


async def ingest_node(state: dict) -> dict:
    rule_id = state["rule_id"]
    rules_dir = Path(state.get("rules_dir") or cfg.RULES_DIR)
    artifacts_dir = Path(state.get("artifacts_dir") or cfg.ARTIFACTS_DIR)

    if state.get("rule_statement"):
        # Caller supplied the rule inline (an API layer, a one-off experiment).
        rule = {
            "title": state.get("rule_title", ""),
            "statement": state["rule_statement"],
            "scope_note": state.get("rule_scope_note", ""),
            "source_clauses": state.get("source_clauses") or [],
            "references": state.get("rule_references") or {},
            "terms": state.get("rule_terms") or [],
        }
    else:
        rule = load_rule(rule_id, rules_dir)

    rulebook = Rulebook(state.get("rulebook_dir") or cfg.RULEBOOK_DIR)
    context = rulebook.resolve(
        references=rule.get("references") or {},
        clause_ids=rule.get("source_clauses") or [],
        term_ids=rule.get("terms") or [],
    )
    rendered = render_context(context)
    logger.info(
        "Rule %s: resolved %d clause(s), %d table(s), %d equation(s), %d figure(s), "
        "%d term(s)%s",
        rule_id, len(context.clauses), len(context.tables), len(context.equations),
        len(context.figures), len(context.terms),
        f" — UNRESOLVED: {context.missing}" if context.missing else "",
    )

    base_models = state.get("base_models") or [str(p) for p in sorted(cfg.IFC_DIR.glob("*.ifc"))]
    if not base_models:
        raise FileNotFoundError(f"no IFC models found in {cfg.IFC_DIR}")

    logger.info("Rule %s: ingested (%d base model(s))", rule_id, len(base_models))
    return {
        "rule_title": str(rule.get("title", "")),
        "rule_statement": str(rule.get("statement", "")),
        "rule_scope_note": str(rule.get("scope_note", "")),
        "source_clauses": list(rule.get("source_clauses") or []),
        "rule_context": rendered,
        "unresolved_references": list(context.missing),
        "rules_dir": str(rules_dir),
        "artifacts_dir": str(artifacts_dir),
        "base_models": base_models,
        "spec_revisions": int(state.get("spec_revisions") or 0),
        "status": "ingested",
    }
