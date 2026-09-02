"""Loading and reference resolution for the rulebook.

Everything is read lazily and cached: a run touches a handful of clauses and
at most one table, so eager loading would be wasted work. Lookups are total —
a missing reference returns ``None`` and is reported, never raised — because a
dangling citation in a 60-year-old code document is a data problem to surface,
not a reason to kill a rule.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bnbc import config as cfg

logger = logging.getLogger("bnbc.rulebook.store")

#: How a citation is written in the code text. This is the contract the
#: rulebook producer must match (docs/RULEBOOK-FORMAT.md): what the agent can
#: look up is exactly what the regulation can cite.
REFERENCE_PATTERNS = {
    "tables": re.compile(r"Table\s*(6\.8\.\d+)", re.I),
    "figures": re.compile(r"Fig(?:ure)?\.?\s*(6\.8\.\d+)", re.I),
    "equations": re.compile(r"Eq(?:uation|n)?s?\.?\s*\(?(6\.8\.\d+)\)?", re.I),
    "clauses": re.compile(r"Sec(?:tion)?s?\.?\s*(8\.\d+(?:\.\d+)*)", re.I),
}


#: How far a citation chain is followed. 2 covers rule -> section -> formula,
#: which is the deepest chain in Chapter 8; higher would start pulling in
#: loosely-related sections for no gain.
MAX_REFERENCE_DEPTH = 2


class RulebookError(Exception):
    """The rulebook is missing or unreadable — a setup fault, not a rule fault."""


def find_references(*texts: str) -> dict[str, list[str]]:
    """Citations appearing in ``texts``, keyed by kind."""
    blob = " ".join(t for t in texts if t)
    found = {kind: sorted(set(rx.findall(blob))) for kind, rx in REFERENCE_PATTERNS.items()}
    return {kind: values for kind, values in found.items() if values}


@dataclass
class ResolvedContext:
    """Everything a rule cites, resolved. ``missing`` is never silently empty:
    a citation the rulebook cannot satisfy must reach the run's log."""

    clauses: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    equations: list[dict[str, Any]] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class Rulebook:
    """Read access to ``rulebook/``."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else cfg.RULEBOOK_DIR
        self._sections: dict[str, dict] | None = None
        self._clause_index: dict[str, tuple[str, dict]] | None = None
        self._terms: dict[str, dict] | None = None
        self._cache: dict[tuple[str, str], dict | None] = {}

    # -- sections and clauses -----------------------------------------
    def _load_sections(self) -> dict[str, dict]:
        if self._sections is None:
            directory = self.root / "clauses"
            if not directory.exists():
                raise RulebookError(
                    f"no rulebook at {self.root} — it is produced by the parser; "
                    f"see docs/RULEBOOK-FORMAT.md"
                )
            sections, index = {}, {}
            for path in sorted(directory.glob("*.json")):
                payload = self._read(path) or {}
                sections[payload.get("section_id", path.stem)] = payload
                for clause in payload.get("clauses", []):
                    index[clause["clause_id"]] = (payload.get("section_id", path.stem), clause)
            self._sections, self._clause_index = sections, index
        return self._sections

    def section(self, section_id: str) -> dict | None:
        return self._load_sections().get(section_id)

    def clause(self, clause_id: str) -> dict | None:
        self._load_sections()
        entry = (self._clause_index or {}).get(clause_id)
        return entry[1] if entry else None

    def resolve_clause_ref(self, ref: str) -> list[dict]:
        """A citation may name a whole section (``Sec 8.3.5.4``) or one clause."""
        clause = self.clause(ref)
        if clause:
            return [clause]
        section = self.section(ref)
        return list(section.get("clauses", [])) if section else []

    # -- artifacts -----------------------------------------------------
    def table(self, table_id: str) -> dict | None:
        return self._artifact("tables", table_id)

    def figure(self, figure_id: str) -> dict | None:
        payload = self._artifact("figures", figure_id)
        if payload and payload.get("image"):
            payload = dict(payload, image_path=str(self.root / "figures" / payload["image"]))
        return payload

    def equation(self, equation_id: str) -> dict | None:
        return self._artifact("equations", equation_id)

    def _artifact(self, kind: str, ref: str) -> dict | None:
        key = (kind, ref)
        if key not in self._cache:
            self._cache[key] = self._read(self.root / kind / f"{ref}.json")
        return self._cache[key]

    # -- terms ---------------------------------------------------------
    def terms(self) -> dict[str, dict]:
        if self._terms is None:
            payload = self._read(self.root / "terms.json") or {}
            self._terms = {t["term_id"]: t for t in payload.get("terms", [])}
        return self._terms

    # -- resolution ----------------------------------------------------
    def resolve(
        self,
        references: dict[str, list[str]] | None = None,
        *,
        clause_ids: Iterable[str] = (),
        term_ids: Iterable[str] = (),
        depth: int = MAX_REFERENCE_DEPTH,
    ) -> ResolvedContext:
        """Resolve a rule's own clauses plus everything it cites, transitively.

        Citations chain: rule 8.3.7.2 requires "one-half the amount required by
        Sec 8.3.5.4(a)", and that section defines the amount by Eq. 6.8.6. One
        hop would hand the drafter a cross-reference to a formula it cannot
        see, which is precisely the situation that makes a model invent one.

        The walk is breadth-first, bounded by ``depth`` and by a seen-set, so a
        citation cycle terminates and a densely cross-referenced section cannot
        drag the whole chapter into the prompt.
        """
        out = ResolvedContext()
        seen: set[tuple[str, str]] = set()

        def take_clause(clause_id: str, clause: dict) -> dict | None:
            """Add a clause once; return it when it is new (so we follow it)."""
            if ("clauses", clause_id) in seen:
                return None
            seen.add(("clauses", clause_id))
            out.clauses.append(clause)
            return clause

        # Level 0: the clauses this rule is made of, in the order it lists them.
        frontier: list[dict[str, Any]] = []
        for cid in clause_ids:
            clause = self.clause(cid)
            if clause is None:
                out.missing.append(f"clause {cid}")
            elif take_clause(cid, clause) is not None:
                frontier.append(clause.get("references") or {})

        pending: dict[str, list[str]] = {}
        for kind, refs in (references or {}).items():
            pending.setdefault(kind, []).extend(refs)

        for level in range(depth):
            for block in frontier:
                for kind, refs in block.items():
                    pending.setdefault(kind, []).extend(refs)
            frontier = []
            if not pending:
                break
            current, pending = pending, {}

            for kind, refs in current.items():
                for ref in refs:
                    if (kind, ref) in seen and kind != "clauses":
                        continue
                    if kind == "clauses":
                        found = self.resolve_clause_ref(ref)
                        if not found:
                            if ("clauses", ref) not in seen:
                                seen.add(("clauses", ref))
                                out.missing.append(f"clause {ref}")
                            continue
                        for clause in found:
                            if take_clause(clause["clause_id"], clause) is not None:
                                # Only newly-seen clauses extend the frontier —
                                # this is what makes a citation cycle terminate.
                                frontier.append(clause.get("references") or {})
                    elif kind in ("tables", "figures", "equations"):
                        seen.add((kind, ref))
                        payload = getattr(self, kind[:-1])(ref)
                        if payload is None:
                            out.missing.append(f"{kind[:-1]} {ref}")
                        else:
                            getattr(out, kind).append(payload)
                    else:
                        out.missing.append(f"unknown reference kind {kind!r}")

        known_terms = self.terms()
        out.terms = [known_terms[t] for t in term_ids if t in known_terms]
        if out.missing:
            logger.warning("rulebook: unresolved reference(s): %s", ", ".join(out.missing))
        return out

    # -- io ------------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RulebookError(f"unreadable rulebook file {path}: {exc}") from exc


_default: Rulebook | None = None


def get_rulebook() -> Rulebook:
    """The process-wide rulebook rooted at ``config.RULEBOOK_DIR``."""
    global _default
    if _default is None:
        _default = Rulebook()
    return _default
