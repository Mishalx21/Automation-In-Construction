# Rulebook format

`rulebook/` is the agent's source of truth for regulatory content. It is an
**input**: this repository reads it and never writes it, and never produces
it. Whoever parses the building code emits this directory; the contract below
is all that has to be agreed.

The reader is `bnbc/rulebook/` (`Rulebook.resolve()` + `render_context()`), and
`tests/test_rulebook.py` asserts these invariants against whatever build is
checked in — so a rulebook that violates this document fails the test suite
rather than surfacing as a bad checker weeks later.

Fields not listed here are ignored. Every field listed *is* read by the agent.

## Why the shape is what it is

1. **A checkable rule is a group of clauses, not a clause.** `8.1.2.1.C`
   covers clauses (c)(i)–(c)(iv); `8.3.4.1.B.D` covers (b)–(d). Clause text and
   rule definition are therefore separate objects in separate directories —
   `rules/<id>/rule.json` names clause ids, the rulebook holds their text.
2. **Nothing an agent reads may carry an image.** Figures are files on disk
   with metadata beside them; an inline base64 blob is context poison.
3. **One representation per artifact, and it is the one a model reads best.**
   Tables arrive as markdown, equations as readable text, inline math already
   normalised (`90°`, not `$90^{\circ}$`; `f'_c`, not `f _ {c} ^ {\prime}`).
   A model given both LaTeX and a rendering spends attention deciding which to
   trust.

## Layout

```
rulebook/
├── clauses/<section_id>.json      e.g. 8.1.2.1.json, 8.3.5.4.json
├── tables/<table_id>.json         e.g. 6.8.1.json
├── equations/<equation_id>.json   e.g. 6.8.6.json
├── figures/<figure_id>.json       e.g. 6.8.2.json
├── figures/<figure_id>.png        the image itself
└── terms.json
```

File names *are* the identifiers: `Rulebook.table("6.8.1")` reads
`tables/6.8.1.json`. Ids must match how the code cites them.

## Citations

References are discovered by scanning clause and statement text with the
patterns in `bnbc/rulebook/store.py::REFERENCE_PATTERNS`:

| Kind | Written as | Resolves to |
|---|---|---|
| `tables` | `Table 6.8.1` | `tables/6.8.1.json` |
| `figures` | `Figure 6.8.2`, `Fig. 6.8.2` | `figures/6.8.2.json` |
| `equations` | `Eq. 6.8.6`, `Equation 6.8.6` | `equations/6.8.6.json` |
| `clauses` | `Sec 8.3.5.4`, `Section 8.1.9.4` | a clause id, or a whole section |

Resolution is **transitive**, breadth-first, bounded by
`MAX_REFERENCE_DEPTH` (2) and a seen-set, so citation cycles terminate:
rule 8.3.7.2 requires "one-half the amount required by Sec 8.3.5.4(a)", and
that section defines the amount by Eq. 6.8.6 — the drafter gets both.

> **A clause's `references` must include artifacts attached to it, not only
> those its prose names.** Clause 8.3.5.4.a.(i) says "*the following
> equation*" and never writes "Eq. 6.8.6". Text-scanning cannot see that link,
> so the producer must record it. Getting this wrong is silent: the drafter
> receives a clause that depends on a formula it cannot see, which is exactly
> the situation that makes a model invent one.

**Unresolvable citations are reported, never invented.** `resolve()` returns
them in `missing`, `render_context()` prints them under "Unresolved
references" with an instruction not to fabricate the content, and `ingest`
logs them. A rulebook with a known gap beats one with a plausible guess.

## Schemas

### `clauses/<section_id>.json`

```jsonc
{
  "section_id": "8.1.6",
  "title": "Spacing of Reinforcement",
  "page": 7,
  "clauses": [
    {
      "clause_id": "8.1.6.4",       // globally unique; how rules cite it
      "text": "Clear distance limitation between bars shall apply also …",
      "notes": "",                   // definitions/commentary, may be ""
      "references": {"equations": ["6.8.6"]},  // cited AND attached artifacts
      "page": 7                      // required: every clause must be citable
    }
  ]
}
```

Clauses must be in **reading order** — they are quoted in the order given, and
a rule reading (c)(ii) before (c)(i) invites a wrong interpretation.

`page` is load-bearing: it renders as "BNBC p.7", so a generated checker
traces back to a page of the code.

### `tables/<table_id>.json`

```jsonc
{
  "table_id": "6.8.1",
  "title": "Table 6.8.1: Minimum Diameters of Bend",
  "markdown": "| Bar Size | Minimum Diameter of Bend |\n| --- | --- |\n| 10 mm ≤ d_b ≤ 25 mm | 6d_b |",
  "notes": [],
  "page": 5
}
```

Markdown, not HTML: cell text uses the same normalisation as clauses (`d_b`,
not `d<sub>b</sub>`; `≤`, not `&le;`).

### `equations/<equation_id>.json`

```jsonc
{
  "equation_id": "6.8.6",
  "readable": "ρ_s = (0.12 f'_c)/(f_yt)",
  "definitions": [{"symbol": "f'_c", "definition": "specified compressive strength"}],
  "page": 36
}
```

`definitions` may be empty, but a symbol table is the cheapest way to stop a
drafter guessing what `h_x` means.

### `figures/<figure_id>.json` + `.png`

```jsonc
{
  "figure_id": "6.8.2",
  "caption": "Figure 6.8.2 Flexural Requirements for Flexural Members of Special Moment Frames",
  "image": "6.8.2.png",   // file name, resolved next to this JSON
  "page": 31
}
```

Only the caption currently reaches a prompt (the present 19 rules state every
threshold in text); the image is on disk ready for a vision path — see TODO.

### `terms.json`

```jsonc
{
  "terms": [
    {
      "term_id": "clear_spacing",    // slug; how rules cite it
      "term": "Clear spacing",
      "symbol": "",                   // optional, e.g. "l_o"
      "definition": "The unobstructed distance between adjacent parallel bars."
    }
  ]
}
```

One entry per concept. A drafter shown two wordings of one concept has to
decide which is authoritative, which is work it should not be doing.

## Scope

The checked-in rulebook covers the transitive closure of the 19 checkable
rules: 46 clauses across 13 sections, plus the artifacts they cite. Two
sections (`8.1.9.4`, `8.3.5.4`) are present because rules cite them, not
because any rule checks them — a rulebook may always contain more than the
rules need.

Adding rules means emitting their sections here and naming the clause ids in a
new `rules/<id>/rule.json`. Nothing in `bnbc/` changes.
