"""The BNBC rulebook — verbatim regulatory content, addressable by reference.

``rulebook/`` on disk is the source of truth: clauses, the tables, figures and
equations they cite, and the defined terms. It is produced by the parser and
is read-only here — the agent never writes it, so every generated checker
traces back to a clause and a page of the code. The format it must satisfy is
``docs/RULEBOOK-FORMAT.md``.

This package is the only way the agent reads it:

* :class:`Rulebook` — load and resolve references (``Table 6.8.1``, ``Sec 8.3.5.4``)
* :func:`render_context` — turn resolved references into prompt text
"""

from bnbc.rulebook.render import render_context, render_reference
from bnbc.rulebook.store import (
    ResolvedContext,
    Rulebook,
    RulebookError,
    find_references,
    get_rulebook,
)

__all__ = [
    "ResolvedContext",
    "Rulebook",
    "RulebookError",
    "find_references",
    "get_rulebook",
    "render_context",
    "render_reference",
]
