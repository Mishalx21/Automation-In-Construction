# BNBC Checking-Code Generator (FIV)

Generates Python rule checkers for BNBC (Bangladesh National Building Code)
provisions and runs them against IFC building models. The core idea is
**FIV — Fixture-Injected Verification**: instead of asking an LLM to judge
whether generated code is correct (no labels, circular, self-preferring),
the agent *manufactures* ground truth by perturbing IFC models into fixtures
with known verdicts, and accepts a checker only when it kills every injected
violation, stays quiet on compliant variants, and reports
`unknown`/`not_applicable` correctly on degraded ones.

Everything is filesystem-based and split by role: the regulation
(`rulebook/`), the rule definitions (`rules/`), the IFC corpus (`models/`),
and everything a run produces (`artifacts/`) are four separate directories.
A run is fully reproducible and diffable, with no database, object store, or
service to stand up.

## Pipeline

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design record,
forensic evidence, and literature review.

```
ingest → spec_card → fixture_plan → retrieve → agentic_draft
    → execute → route:
          accepted    → conformance (advisory) → store → END
          oracle fault→ spec_revise → fixture_plan (bounded: MAX_SPEC_REVISIONS)
          else        → reject → END
```

`fixture_plan` can also loop back to `spec_card` on `plan_invalid` (bounded by
`MAX_PLAN_REGENS`).

- **ingest** — loads `rules/<id>/rule.json` and resolves it against the
  `rulebook/`: the BNBC clauses the rule checks, plus every table, figure,
  equation and cross-referenced section they cite. This is the only door
  regulatory text enters through.
- **spec_card** — one strong-model call turns the rule text into a versioned
  interpretation contract (`artifacts/<id>/spec_card.yaml`): RASE-style
  conditions, resolved measurement conventions, data prerequisites, and a
  machine-executable fixture plan. A card already on disk wins entirely, so
  human corrections are possible offline — but nothing ever waits on one.
  Conventions that gate applicability must declare how the card's own
  fixtures satisfy them (`fixture_discharge`), so a convention can never
  silently poison the oracle.
- **fixture_plan** — `bnbc/fixtures` validates the plan against its operator
  vocabulary, materialises the fixtures under `artifacts/fixtures/<id>/`, and
  **self-verifies** every file by independently re-measuring the perturbation
  (`bnbc/fixtures/measure.py` never imports `ifc_helpers`, so a helper bug
  cannot confirm a fixture bug).
- **retrieve** — the live `ifc_helpers` API reference, the most similar
  previously-accepted checker (lexical, with a relevance floor; a rule never
  retrieves itself), and a census-based profile of what the real corpus
  actually contains.
- **agentic_draft** — a tool-using LLM session (≤ `AGENT_MAX_TURNS` turns)
  with four tools: `inspect` (read-only IFC probe), `evaluate` (static gate
  + fixture gate scorecard), `fetch_context` (resolve a BNBC reference out of
  the rulebook), `run_on_real` (advisory run on a production model). Tools
  live in `bnbc/agent/tools.py` as one registry. The best
  candidate across all iterations is preserved (elitism).
- **execute + gate** — the candidate runs in isolated subprocesses on the
  real corpus and on every fixture. `bnbc/fixtures/gate.py` is the only place
  `accepted=True` can be computed: 100% kill rate with correct condition ids
  and element GUIDs, zero false positives, correct `unknown`/`not_applicable`,
  schema-valid output, inside the timeout. A failed gate is classified as a
  **code fault** (→ reject, with the best draft handed off) or an **oracle
  fault** (systematic verdict-class mismatch, e.g. every fixture
  `not_applicable` → the spec, not the code, is broken → `spec_revise`).
- **spec_revise** — bounded outer loop (default 1): revises the spec card
  from the gate's evidence when no code edit can fix the mismatch, then
  rebuilds fixtures and re-drafts from scratch.
- **conformance** — one bounded, separate-model structured review of the
  *accepted* code, entirely off the critical path: advisory provenance flags
  only; a reviewer failure can never cost a gate-accepted checker.
- **reject** — terminal, no human gate: `artifacts/<id>/rejection.json`
  carries the structured reason, fault classification, and full gate
  evidence, and `artifacts/<id>/partial/` carries the best runnable draft.

Verdicts are four-valued (`pass | fail | unknown | not_applicable`,
`bnbc/contracts.py::CheckResultV2`) with per-condition coverage accounting —
a checker that examined zero elements can never return `pass`.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # set GEMINI_API_KEYS (comma-separated pool)
```

```bash
python scripts/run_v2.py 8.1.2.1.A
```

`--all` runs every rule under `rules/`. Output lands in `artifacts/<id>/`:
either `checker.py` + `acceptance.json` + `provenance.json` (accepted) or
`rejection.json` + `partial/` (rejected). `rules/` is never written to.

The rulebook is checked in, so a fresh clone runs as-is.

Tests: `pytest tests/ -q` (`-m "not slow"` skips the subprocess-heavy gate
tests; CI runs the fast tier).

## LLM providers

`bnbc/llm/` is a provider-agnostic layer: nodes speak only `Message` /
`ToolSpec` / `LLMResponse`, and `LLM_PROVIDER` decides who answers.

| Provider | Module | Resilience |
|---|---|---|
| `gemini` (default) | `bnbc/llm/gemini.py` | multi-key pool with rotation and dead-key exclusion, **per-model cooldown rotation** (see below), config degradation when a schema dialect is rejected |
| `openrouter` | `bnbc/llm/openrouter.py` | OpenRouter's own upstream routing + per-family structured-output dialect |

Adding a backend is one `LLMProvider` subclass plus an entry in
`config._MODEL_DEFAULTS`. `call_llm` (in `bnbc/llm/call.py`) adds the
cross-provider guarantees: wall-clock ceiling, metering of failed calls,
schema-less retry, and a token budget that can abort a rule.

Gemini enforces quota **per model**, so the fallback chain is a throughput
budget, not just a failure path: a rate-limited or overloaded model is parked
for `GEMINI_MODEL_COOLDOWN_SECONDS` and the next one takes over, rather than
being retried through the whole key pool while other models sit idle. If every
model is cooling the provider waits for the first to free up — waiting beats
failing, and `call_llm`'s wall-clock ceiling is the outer bound. Adding a model
to `GEMINI_MODEL_FALLBACKS` adds its quota to the pool.

The default chain is the text models measured against the three things that
actually break models here — native structured output, tool calling, and the
`SpecCard` schema with its free-form dict fields — at flash latency:
`gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3-flash-preview`,
`gemini-2.5-flash`. Deliberately absent: `*-pro` (capable, but over 75 s on a
trivial call, which would blow the drafter session's wall clock) and `*-lite`
(they pass the capability probes, but "as good as 2.5-flash at writing a
checker" is unmeasured).

Costs come from `bnbc/llm/model_prices.json`. A model with no listed price
does **not** silently cost nothing: the run is flagged `cost_is_partial` and
names the unpriced models, so a reported figure is never mistaken for a
complete one. Add real published rates there as you confirm them.

## Layout

```
bnbc/
├── contracts.py         # shared data contracts (verdicts, spec cards, fixtures, gate)
├── config.py            # paths, model selection, every loop/budget bound
├── llm/                 # provider-agnostic LLM layer
│   ├── base.py          #   messages, tools, errors, provider interface
│   ├── call.py          #   the metered entry point every node uses
│   ├── gemini.py        #   key pool + key/model rotation
│   ├── openrouter.py    #   OpenAI-compatible backend
│   ├── metering.py      #   token accounting + per-rule budget
│   ├── model_prices.json#   per-model token prices
│   └── registry.py      #   provider selection
├── agent/               # the LangGraph agent
│   ├── graph.py         #   nodes + wiring + failure policy
│   ├── ingest.py        #   rule inputs + rulebook resolution
│   ├── spec_card.py     #   interpretation contract: generate / repair / revise
│   ├── retrieval.py     #   helper reference, exemplars, corpus profile
│   ├── prompts.py       #   drafter brief + code extraction
│   ├── tools.py         #   the drafter's tool registry
│   ├── agentic_draft.py #   the bounded tool session
│   ├── execution.py     #   real-model runs + fault classification
│   ├── conformance.py   #   advisory post-acceptance review
│   └── rule_store.py    #   filesystem persistence (accept + reject artifacts)
├── rulebook/            # read access to rulebook/ (load, resolve, render)
└── fixtures/            # the FIV fixture engine
    ├── operators/       #   registered perturbation operators — pure, self-describing
    ├── measure.py       #   independent measurement oracle (no ifc_helpers)
    ├── selfverify.py    #   post-write re-measurement of every operator claim
    ├── planner.py       #   spec-card sketches -> validated FixtureSpecs
    ├── manifest.py      #   materialise fixtures + manifest.json
    ├── gate.py          #   the acceptance decision
    ├── census.py        #   base-model class census + digest cache
    └── synthetic.py     #   programmatic IFC builders (shared with tests)

ifc_helpers/             # runtime library the GENERATED checkers import

INPUTS — never written by a run
rulebook/                # verbatim BNBC — an input, produced elsewhere
├── clauses/<section>.json
├── tables/<id>.json     #   markdown
├── figures/<id>.json + .png
├── equations/<id>.json  #   readable form
└── terms.json           #   deduplicated glossary
rules/<id>/rule.json     # source_clauses, statement, scope_note, references, terms
models/                  # real IFC corpus

OUTPUTS — everything a run produces, regenerable
artifacts/<id>/          # spec_card.yaml, checker.py, acceptance.json,
                         #   prerequisites.json, provenance.json,
                         #   rejection.json, partial/
artifacts/fixtures/<id>/ # generated fixtures + manifest

scripts/                 # run_v2.py (agent), evaluate_v2.py (external eval)
docs/                    # architecture, design records, paper outline, figures
tests/                   # helpers + fixture engine + LLM + rulebook + agent
```

`ifc_helpers` sits at the repository root on purpose: generated checkers do
`import ifc_helpers`, so it is a public runtime contract, not agent internals.

## The rulebook

A checkable rule is not a BNBC clause — it is a *group* of them. `8.1.2.1.C`
covers clauses (c)(i) through (c)(iv); `8.3.4.1.B.D` covers (b) through (d).
So the regulation and the rule definition are separate objects:

- `rulebook/` holds the code verbatim — clause text, the tables, figures and
  equations it cites, and the defined terms — each citable to a page. It is
  an **input**: produced elsewhere to the contract in
  [docs/RULEBOOK-FORMAT.md](docs/RULEBOOK-FORMAT.md), read-only here.
  Parsing the building code is not this repository's job.
- `rules/<id>/rule.json` names the clauses a rule checks, states what must
  hold, and cites what it needs. `ingest` resolves those citations, so the
  drafter sees both the curated statement and the code's own words — and when
  they could be read differently, the clause wins.

References resolve **transitively** (breadth-first, depth-bounded, cycle-safe):
rule 8.3.7.2 requires "one-half the amount required by Sec 8.3.5.4(a)", and
that section defines the amount by Eq. 6.8.6 — the drafter gets the clauses
*and* the formula. Anything the closure misses is one `fetch_context` call
away, and a reference the rulebook cannot satisfy is reported as unresolved
rather than invented.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `openrouter` |
| `GEMINI_API_KEYS` | — | key pool, comma-separated (required for `gemini`) |
| `GEMINI_MODEL_FALLBACKS` | 4 flash models | rotation chain; each contributes its own quota |
| `GEMINI_MODEL_COOLDOWN_SECONDS` | `60` | how long a rate-limited model is parked |
| `OPENROUTER_API_KEY` | — | required for `openrouter` |
| `SPEC_MODEL` / `DRAFTER_MODEL` / `REVIEWER_MODEL` | provider defaults | blank = the active provider's default |
| `DRAFTER_REASONING_EFFORT` | `medium` | reasoning cap for draft calls |
| `SPEC_REASONING_EFFORT` | `none` | spec stage outputs data, not proofs |
| `AGENT_MAX_TURNS` | `24` | LLM turns per drafter session |
| `AGENT_MAX_INSPECTS` | `10` | read-only IFC probes per session |
| `AGENT_WALL_TIMEOUT_SECONDS` | `1800` | wall-clock ceiling for the session |
| `AGENT_CONTEXT_TOKENS` | `96000` | context ceiling before force-end |
| `MAX_PLAN_REGENS` | `2` | plan-invalid spec-card regeneration bound |
| `MAX_SPEC_REVISIONS` | `1` | outer spec-revision bound (oracle faults) |
| `ORACLE_FAULT_MISS_RATIO` | `0.9` | miss fraction that flags an oracle fault |
| `TOKEN_BUDGET_PER_RULE` | `500000` | hard cap (alert at `TOKEN_ALERT_PER_RULE`) |
| `CHECKER_TIMEOUT_SECONDS` | `120` | per-subprocess execution timeout |
| `EXEC_MAX_WORKERS` | `4` | concurrent checker subprocesses |
| `ENABLE_CONFORMANCE_REVIEW` | `false` | advisory review after gate acceptance |
| `LANGSMITH_TRACING` | `false` | trace graph nodes and LLM calls to LangSmith |

## Evaluation

Evaluation against labels is **external by design**: the agent's core claim is
that it produces correct checkers *without* labeled test cases.

This repository holds no labels at all. The labelled cases and the IFC files
they name live in the **Code-Agent** repository, and one dev-only script reads
them from there:

```bash
python scripts/evaluate_v2.py --all
```

Clone Code-Agent as a sibling directory, or point `--code-agent` /
`CODE_AGENT_ROOT` at it. Tests assert that no `rules/*/rule.json` carries test
cases, that no live string in `bnbc/` reaches for them, and that
`evaluate_v2.py` is the only script that does.

The last v1-capable commit is tagged `v1-baseline` and the last
multi-drafter commit `v2-multidrafter` — the paper's baseline and ablation
arms.
