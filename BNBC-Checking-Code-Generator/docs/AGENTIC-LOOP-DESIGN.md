# Path A — Agentic Draft Loop (final analysis + design, 2026-07-17)

**Decision context.** After a full day of live runs (10+ runs, 3 external
evaluations, the Code-Agent harness study), the user chose Path A: keep the
pipeline's oracle factory (spec card → self-verified fixtures → deterministic
gate) and replace the blind draft→repair stage with an **IDE-harness-style
agentic loop implemented as LLM tool calls**. This document is the final
consolidated issue list and the design.

---

## 1. Final issue inventory (every defect class observed, with status)

### Fixed earlier today (v3.2 + evening commits — kept, not revisited)
| Class | Fix |
|---|---|
| Token runaways, unmetered failures, terminal engine crashes, fixture bloat/duplication/contradictions, spec YAML re-injection, unstated gate contracts, feedback over-truncation, exemplar pollution, helper-totality churn, plan-budget interplay, column orientation, vocabulary truncation, data-location/units/arithmetic oracle bugs, copy-host relationships | ledger I1–I19 + commits `6515a14`, `eaa2660`, `fe1bb76` |

### Open issues this design addresses
| # | Issue | Evidence | Design answer |
|---|---|---|---|
| O1 | **Drafter is blind**: cannot look at actual IFC content before/while coding | Code-Agent's ~130 scratch probes are the fossil record of why looking matters; our drafts repeatedly guessed wrong (local-vs-world frames, pset locations) | `inspect` tool: sandboxed read-only Python probe against any fixture/base model |
| O2 | **Whole-module rewrite repair oscillates** | 6/11→5/11→5/11 live; elitism mitigates but cannot converge | Persistent-context agentic loop: the model keeps its own code in context, edits incrementally, and re-evaluates — the mechanism that converges in the IDE |
| O3 | **3-round cap cuts converging runs** | 8.3.4.2 monotone 2→3→4→5 then stop | Loop bounded by turns/tokens/wall-clock, not "rounds": typically 10–25 cheap tool turns ≈ same cost as 2 old rounds |
| O4 | **Feedback loses file-level attribution and tracebacks** | distilled prose vs Code-Agent's per-file scorecard + on-demand traceback | `evaluate` tool returns the FULL structured per-fixture scorecard (expected/actual/conditions/elements/checker_summary/measured ground truth) + traceback tails |
| O5 | **Oracle distribution too narrow** (IFC4/mm/indexed synthetics vs Revit IFC2X3 composite reality) | 8.1.2.2 gate-accepted then 3/40 external (every real hook "violating"); 8.3.5.1 0/4 until bbox-fallback adjudication | (a) spec prompt requires a composite-curve variant fixture for bar-geometry rules (engine already supports `curve_style="composite"`); (b) `run_on_real` advisory tool lets the agent sanity-check false-positive storms on the unlabeled corpus — the Step-5.5 analog |
| O6 | **Fresh-context repair re-pays the full prompt every round** | ~12K input/round | One session, growing but tool-output-capped context; cheaper per iteration than a fresh 12K prompt + full module regeneration |

### Explicitly rejected (overkill / evidence against)
- Consensus / parallel drafters (v3 forensics; ablation arm only).
- Embedding retrieval (library of 2).
- An LLM "card doctor" replacing human adjudication (compliance products want the signature; disk-wins works).
- Unbounded agent autonomy: every tool is sandboxed, metered, wall-clocked; acceptance authority stays with the deterministic gate ONLY.

## 2. Why showing the agent the fixture expectations is legitimate here

Code-Agent iterates against labeled answers — defensible only because a human
curated those labels. FIV's expectations are **manufactured ground truth**:
the fixtures were built by injection and independently self-verified, so
"overfitting to the fixtures" literally means *implementing the spec card*.
Generalization is enforced elsewhere: fixture idiom diversity (O5a), the
advisory real-corpus check (O5b), and the external evaluation which the loop
never sees.

## 3. Design

### 3.1 Graph change (minimal)

```
ingest → spec_card → fixture_plan → retrieve → agentic_draft → execute ─┬→ conformance → store
                          ↑______________________________________________├→ spec_revise (oracle fault, ≤1)
                                                                          └→ reject (best draft + needs_human)
```

`agentic_draft` replaces `draft` + `repair` + the execute↔repair loop. The
`execute` node is unchanged and remains the **sole acceptance authority** —
it re-runs the agent's final candidate from scratch (defense against any
in-loop bookkeeping bug). Routing after execute: accepted → store;
oracle-fault → spec_revise (which re-enters agentic_draft with the new
oracle); else reject with the loop's best candidate (elitism preserved).

### 3.2 The loop (one node, internal tool cycle)

Model: `DRAFTER_MODEL` (deepseek-v4-pro) with native tool calling,
temperature 0, reasoning `medium`. Tools:

| Tool | Contract | Bounds |
|---|---|---|
| `inspect(target, code)` | Run read-only Python against one fixture file or base model (`target` = fixture_id or model name). `model` pre-opened; stdout returned. Same AST bans as checkers + no writes. | 30 s, stdout ≤ 4 KB, ≤ `AGENT_MAX_INSPECTS` |
| `evaluate(code)` | Static gate + fixture gate on the manifest. Returns per-fixture rows: expected/actual verdict, conditions hit/missed, elements missed (annotated), checker_summary, self-verified measured truth, execution error tail (≤ 2 KB). Tracks the best candidate. | one gate run (~seconds on synthetics) |
| `run_on_real(model_name)` | Advisory: run current best code on one real corpus model; returns verdict + summary + violation count (NO labels — sanity signal only, e.g. "861 of 861 hooks violating" smells like a measurement bug). | 120 s |

Loop protocol (system prompt): inspect the fixtures you don't understand →
write the module → `evaluate` → fix what the scorecard names → repeat;
before finishing, `run_on_real` once and investigate anomalies; stop when
`evaluate` reports accepted.

Termination (all env-tunable): gate accepted, or `AGENT_MAX_TURNS` (24), or
token budget (shared `TOKEN_BUDGET_PER_RULE`), or `AGENT_WALL_TIMEOUT`
(30 min). On termination without acceptance: the best-scoring candidate
(elitism, same `candidate_score`) goes to `execute` → likely reject →
best-draft + `needs_human_intervention` handoff, as designed.

Context control: tool outputs are hard-capped (4 KB / 2 KB tails); the
conversation carries the agent's own drafts, which is the point (persistent
working memory — O2/O6). A final safety: if the running context estimate
exceeds `AGENT_CONTEXT_TOKENS` (96 K), the loop force-finishes with the best
candidate.

### 3.3 Cost/latency envelope

Per turn: ~1–6 K in / 0.5–4 K out (deepseek ≈ $0.0004–0.004). A 15-turn
session ≈ 60–150 K tokens ≈ **$0.05–0.15**, comparable to today's
draft+3-repairs, with far higher information per token and no 10-minute
blind generations. Gate runs on synthetic fixtures are seconds each.

### 3.4 What stays untouched

Spec card generation/adjudication, planner (validation, dedupe, trim,
inference), manifest/self-verification, gate, elitism scoring, store/reject
handoff, accounting, all bounds. The paper's oracle contribution is intact;
the generation arm becomes "one tool-using agent, bounded budget, gate-only
acceptance" — strictly simpler to describe than draft+repair routing.
