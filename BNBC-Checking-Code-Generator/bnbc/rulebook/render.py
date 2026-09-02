"""Rendering resolved rulebook content as prompt text.

Markdown, not HTML or LaTeX: a table the model has to parse out of
``<td><sub>`` tags is a table it will misread. Every block carries its clause
or artifact id so the drafter can cite what it used — and so a reviewer can
check the citation.
"""

from __future__ import annotations

from typing import Any

from bnbc.rulebook.store import ResolvedContext

#: A clause quoted in full is worth its tokens; a runaway one is not.
CLAUSE_CHAR_CAP = 2000


def render_clause(clause: dict[str, Any]) -> str:
    text = (clause.get("text") or "").strip()
    if len(text) > CLAUSE_CHAR_CAP:
        text = text[:CLAUSE_CHAR_CAP] + " …(truncated)"
    head = f"**{clause.get('clause_id', '?')}**"
    page = clause.get("page")
    if page:
        head += f" (BNBC p.{page})"
    lines = [f"{head} {text}"]
    if clause.get("notes"):
        lines.append(f"  _note:_ {clause['notes'].strip()}")
    return "\n".join(lines)


def render_table(table: dict[str, Any]) -> str:
    title = table.get("title") or f"Table {table.get('table_id')}"
    lines = [f"**{title}**", table.get("markdown", "").strip()]
    for note in table.get("notes") or []:
        lines.append(f"_note:_ {note}")
    return "\n".join(part for part in lines if part)


def render_equation(equation: dict[str, Any]) -> str:
    lines = [f"**Eq. {equation.get('equation_id')}**: `{equation.get('readable', '').strip()}`"]
    for definition in equation.get("definitions") or []:
        if isinstance(definition, dict):
            symbol = definition.get("symbol") or definition.get("name") or "?"
            lines.append(f"  - `{symbol}`: {definition.get('definition', '')}")
        else:
            lines.append(f"  - {definition}")
    return "\n".join(lines)


def render_figure(figure: dict[str, Any]) -> str:
    """Caption and provenance only.

    The image is on disk (``image_path``) but is not inlined: for these rules
    every numeric requirement is stated in the clause text, so the figure
    confirms rather than defines. The caption tells the drafter what the
    diagram asserts and where to find it.
    """
    parts = [f"**Figure {figure.get('figure_id')}** — {figure.get('caption', '').strip()}"]
    page = figure.get("page")
    if page:
        parts.append(f"(BNBC p.{page}; image at `{figure.get('image', '')}`)")
    return " ".join(parts)


def render_term(term: dict[str, Any]) -> str:
    name = term.get("term", term.get("term_id", "?"))
    symbol = term.get("symbol")
    label = f"{name} ({symbol})" if symbol else name
    return f"- **{label}**: {term.get('definition', '').strip()}"


def render_reference(kind: str, payload: dict[str, Any]) -> str:
    """One resolved artifact as prompt text (used by the fetch_context tool)."""
    return {
        "clause": render_clause, "clauses": render_clause,
        "table": render_table, "tables": render_table,
        "figure": render_figure, "figures": render_figure,
        "equation": render_equation, "equations": render_equation,
        "term": render_term, "terms": render_term,
    }[kind](payload)


def render_context(context: ResolvedContext) -> str:
    """The whole resolved bundle, as one prompt section (empty if nothing)."""
    blocks: list[str] = []
    if context.clauses:
        blocks.append("### Source clauses (verbatim BNBC)\n"
                      + "\n\n".join(render_clause(c) for c in context.clauses))
    if context.tables:
        blocks.append("### Referenced tables\n"
                      + "\n\n".join(render_table(t) for t in context.tables))
    if context.equations:
        blocks.append("### Referenced equations\n"
                      + "\n".join(render_equation(e) for e in context.equations))
    if context.figures:
        blocks.append("### Referenced figures\n"
                      + "\n".join(render_figure(f) for f in context.figures))
    if context.terms:
        blocks.append("### Defined terms\n"
                      + "\n".join(render_term(t) for t in context.terms))
    if context.missing:
        blocks.append("### Unresolved references\n"
                      + "\n".join(f"- {m} (not in the rulebook — do not invent its content)"
                                  for m in context.missing))
    return "\n\n".join(blocks)
