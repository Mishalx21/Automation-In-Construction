# Deep Analysis — Why the App Regressed vs. the IDE Code-Agent, and How to Fix It

**Date:** 2026-07-23
**Repos analysed:** `D:\BNBC-Checking-Code-Generator` (the new application, "FIV" pipeline) and `D:\Code-Agent` (the older IDE-based skill agent).
**Question:** the app fails to generate correct checkers for most rules (last tried **8.3.7.2**); the IDE agent "almost always" produced correct code. What are the real lackings, and what is the minimal high-leverage fix set?

Everything below is grounded in the code and artifacts of both repos (file:line cited), not assumption.

---

## 0. The scoreboard (measured, not claimed)

| | Old Code-Agent | New App (v3.3) |
|---|---|---|
| Rules with a working checker | **19 / 19** | **4 / 19** |
| Aggregate accuracy | 436/436 = **1.00** (`best-solutions/aggregated_metrics.json`) | n/a (no in-repo eval) |
| Stored checkers | 19 best-solutions | `8.1.2.1.A`, `8.1.2.2`, `8.1.6.1`, `8.3.5.1` |
| Rejected | — | `8.1.6.5`, `8.3.7.2` |
| Partial (spec built, never finished) | — | `8.3.4.2` |
| Untouched (rule.json only) | — | 12 |

Note: even the 4 "stored" rules needed manual card adjudication to survive external eval (`8.3.5.1` was **0/4** external until a human rewrote the card; `8.1.2.2` was **3/40**). So the real success rate on *unseen* files is lower than 4/19.

**The two rejections are two different failure modes, both traceable to the redesign:**

| | 8.1.6.5 | 8.3.7.2 |
|---|---|---|
| Failure class | oracle / fixture-contract fault | budget blowout (agentic loop) |
| reason | "fixture plan invalid: IfcWall has no attribute 'Thickness'" | "token budget exhausted in _agentic_draft: 543275 > 500000" |
| repair_rounds / spec_revisions | 3 / 1 (both exhausted) | 0 / 0 (died before drafting finished) |
| candidate produced | yes, glm-5.2, kill_rate 0.0, **11× `except: pass`** | **null** (all work discarded) |
| fixtures | **all 7 are 54–58 MB** full-model perturbations | 7 tiny synthetic + 2 giant (56/55 MB) |
| open adjudication items | 5 | **7 (every convention still `proposed`)** |

---

## 1. Why the OLD Code-Agent reliably worked

Four load-bearing mechanisms, all confirmed in `D:\Code-Agent`:

1. **An authoritative labeled oracle.** `data-v2/Rules/<id>/rule.json` carries human `expected_result` booleans per IFC file. `agent-tools/evaluate_a_rule.py:210` scores deterministically: `tested_answer == expected → match`; a crash/timeout is `None` and **never** counts as a pass (`:90-133`, subprocess-isolated with full traceback piped back). Accuracy is an objective, reproducible number — the agent never had to *guess* whether a file was compliant.

2. **Per-failure diagnostic `reason` fields = the interpretation anchor.** Non-compliant cases carry a `reason` naming the *exact formulas*. For `8.3.7.2_P3` the reason literally spells out `So = 100 + (350 − hx)/3` and `ldh = max(8·db, 150, fy·db/(5.4·√f'c))` — and those formulas appear near-verbatim in `best-solutions/8.3.7.2/check_rule.py:409-411,492-493`. The rule *text* alone never says this; the reason does.

3. **Unbounded, surgical, crash-isolated iteration.** ~200 disposable inspection scripts across `environments/` (`tmp_8.1.6.1` alone has 21). The loop had **no round cap and no token cap** — it iterated until accuracy hit 1.0 (`SKILL.md:126-130`), testing a draft via `--code-path` before promoting it.

4. **Crystallized domain doctrine, broad and exploratory.** `CLAUDE.md` + the 138-line `SKILL.md` pre-solved IFC's sharp edges: directrix-not-`create_shape` for rebar, unit double-scaling guards, Z-range overlap for spliced bars, 1500 mm starter-bar extension, slab aspect-ratio classification, level-by-level coverage, exhaustive per-violation reporting, strict-honesty crash handling, and an explicit anti-overfitting list. Critically it told the agent to *use reasons to understand interpretation but never hardcode them* (`SKILL.md:25`).

The synthesis: **trustworthy binary oracle + rich per-failure diagnostics + crash-isolated promote-after-proof loop + doctrine that pre-solved IFC + mandatory hands-on inspection.** Remove any one and 436/436 does not hold.

---

## 2. What the new app lost — root-cause findings

The app set out to remove one real bottleneck — *needing labeled IFC test cases to drive generation*. That innovation (manufacture fixtures by violation injection) is sound. But the redesign also discarded things that were **not** the bottleneck, and added new failure modes. Findings, most-severe first:

### L1 — The oracle has no external anchor; it grades the model's own guesses. *(deepest)*
The fixture `expected_verdict` is an **LLM-authored field** — populated by the spec-card model (`spec_card.py:268`), passed through untouched (`planner.py:635`, `contracts.py:212`), and graded by the gate (`gate.py:145`). "Self-verification" (`selfverify.py:197-212`) rigorously proves *what the perturbation did to the geometry/properties* (`type_count=0`, `profile_dims`, `placement_origin`, `pset_value`…) — **never the rule verdict.** So the gate certifies a checker precisely when it reproduces the spec model's guesses. This is *internal self-consistency*, not correctness. Worse, the "oracle fault" remedy (`spec_card.py:509-534`) **weakens the card until its own fixtures pass** — moving the oracle to fit the code, which proves it has no external anchor. This is exactly why gate-accepted checkers scored 0/4 and 3/40 externally.

### L2 — There is no interpretation-disambiguation channel that generalizes to new rules.
Dropping `test_cases` in `ingest.py:4` is **correct** and must stay: labels (and their `reason` fields) are labeled data; they cannot enter the agent loop because new rules won't have them, and using them in-loop would neither generalize nor be honest against the dev-time eval (train-on-test leakage). The real lacking is what the app put *in their place*: nothing. For new rules the only interpretation input is `rule_text` + `explanation`, and the app resolves ambiguity by having a **no-reasoning** spec model guess (see L6). The 8.3.7.2 labels are useful only as a **dev-time diagnostic** of how large this gap is: the labeled reasons check **hook development length `ldh`, 90° hook extension, and spacing `So`** (Sec 8.3.5.4), whereas the app's card — from rule text alone — invented a *different* interpretation (beam-column joint intersection volumes, "framing into all four sides," a 75 mm column-core assumption). That divergence is the failure, and it must be closed by a channel that exists for **every** rule (stronger interpretation from text, or an optional author-written interpretation note — see T1), never by feeding the labels back in.

### L3 — Budget exhaustion **discards all accumulated work** (concrete bug).
`agentic_draft.py:396-397` re-raises `BudgetExceeded`, which propagates *past* the candidate-packaging at `:459`. So on 8.3.7.2 the loop ran 19 turns, may have had a best-of-rounds candidate in `best`, and returned **`candidate: null`** — nothing salvaged, `needs_human` with no code to show. Elitism exists but the budget path bypasses it.

### L4 — The agentic loop's context balloons; the cost envelope was mis-estimated.
`AGENTIC-LOOP-DESIGN.md:90-95` budgeted "15 turns ≈ 60–150 K tokens ≈ $0.05–0.15." 8.3.7.2 spent **432 K input + 89 K output across 19 calls, $0.28**. Cause: the design assumed capped tool outputs keep context small, but the agent's **own full module (30–40 KB) is re-sent on every `evaluate` call and persists in every turn**, and giant-fixture scorecards/inspects add more. `AGENT_CONTEXT_TOKENS=96 K` caps a *single* turn but not the *cumulative* replay — ~5–6 full-context turns exhaust the 500 K rule budget, so hard rules cannot converge in 24 turns.

### L5 — Fixture realism gap: pass/fail is only ever tested on ~2–6 KB micro-scenes.
By design (`spec_card.py:140-146`) pass/fail fixtures use tiny `__synthetic__` bases; only NA/unknown perturb the real model. Consequence: **a checker's pass/fail correctness is never demonstrated on a real production model** before storage — the exact gap behind the external-eval misses. And the NA/unknown perturbations that *do* use the real model produce **54–58 MB files** (`delete_elements_of_type`, `strip_pset`, `strip_attribute`, `copy_element_offset` rewrite the whole 56 MB building). Strong correlation in the data: `8.3.5.1` (28 uniform ~2 KB synthetic fixtures, zero giant) stored cleanly; `8.1.6.5` (all 7 fixtures 54–58 MB) was rejected. Giant fixtures are also slow and token-heavy for `inspect`/`evaluate`.

### L6 — The hardest cognitive task runs with reasoning disabled.
`SPEC_REASONING_EFFORT="none"` (`config.py:74`): the stage that authors the interpretation *and* guesses every verdict runs a no-reasoning structured call. This was a workaround for deepseek/glm burning uncapped reasoning tokens — but it means the oracle's soundness (L1) rests on a model told not to think. Memory already flags a reasoning-capable spec model as the highest-leverage untried lever.

### L7 — Doctrine narrowed to an enforced subset; exploratory doctrine dropped.
The drafter prompt (`drafting.py:27-132`) keeps the strong enforced rules (directrix fast-path, unit guard, coordinate-frame trap, GUID attribution, Z-overlap, no-magic-constants, AST-gated `except: pass`). But several old-SKILL doctrines are **absent**: level-by-level/storey coverage, exhaustive multi-violation discovery, the 1500 mm starter-bar extension, slab aspect-ratio one-way/two-way classification, and within-model mixed-unit (mm vs m) detection.

### L8 — No forcing function on interpretation confidence; degraded-model fallback ships bad code.
Cards flow into drafting fully unadjudicated (all 7 conventions `proposed` on 8.3.7.2) with nothing gating on "how much of this is a guess." Separately, after 2 parse failures the drafter falls back to `z-ai/glm-5.2` (`config.py:54`, known-broken) — which is what produced 8.1.6.5's `except: pass`-riddled 0.0-kill candidate.

---

## 3. Improvement roadmap (tiered; deliberately *not* overkill)

**Hard constraint (from the project owner).** Labeled test cases must **never** enter the agent loop. The whole point of the app is to produce correct checkers for *new* rules that have **no** labels — if a rule needed labels in-loop, you would just run the IDE Code-Agent and this app would be redundant. Therefore:

- The 19 labeled rules are a **held-out test set for the architecture**, used only at **development time** via the existing external script `scripts/evaluate_v2.py` (runs stored `checker.py` against `rule.json` `test_cases` in the shared IFC pool, drafter-blind). It is the *architecture-effectiveness meter*, not an acceptance gate.
- Every fix below must be **generic** — it improves how the pipeline handles *any* rule from `rule_text`/`explanation`, and is scored by watching `evaluate_v2.py` on the 19 rules go up **without** any per-rule label leakage. (Standard train/test discipline: touching the loop with the 19 rules' labels or reasons would invalidate that number.)

**My earlier T2 — "validate accepted checkers against the labeled cases inside the pipeline" — is RETRACTED.** It reintroduces the exact dependency the app exists to remove.

### Tier 1 — Do these first (highest leverage, low cost, low risk)

**T1. Autonomous interpretation grounding — reference resolution + knowledge retrieval (addresses L2, NO human input).**
*(Revised 2026-07-23 after the owner ruled out any human interpretation channel — see Part II below for the full research. Superseded idea: author-written `interpretation_notes` — RETRACTED.)*
- (a) **Deterministic local cross-reference resolver.** When `rule_text` cites "Sec X.Y", look up `rules/X.Y*/rule.json` and inject the referenced text into the spec prompt. Zero LLM, zero human. This alone would have given 8.3.7.2's spec model the actual 8.3.5.4 spacing/placement text instead of forcing it to invent joint conventions.
- (b) **Curated code-corpus retrieval + bounded web search at spec time.** Keep a local copy of the BNBC 2020 Chapter 8 text (public document) as a retrieval corpus; wire the already-present-but-unused `TAVILY_API_KEY` as a bounded spec-stage search tool for anything not local (e.g. the `ldh = fy·db/(5.4·√f'c)` hook-development formula that exists in *no* local rule.json). Version trap verified: the freely hosted BNBC **2012** text differs from **2020** (8.3.5.4(b): "≤ ¼ min dimension nor 100 mm" vs 2020's `s_o = 100+(350−hx)/3` ≤150 cap) — prefer the curated 2020 corpus; treat live web as fallback.
- (c) **No-invented-constants rule.** A spec convention may only use numbers traceable to rule text, a resolved reference, or a retrieved citation; anything else (8.3.7.2's invented "75 mm cover" core fallback) must become `on_missing`/`unknown`, never a made-up constant.

**T2. Use `scripts/evaluate_v2.py` as the development feedback loop (fixes how you detect L1).**
You cannot anchor the oracle with labels *in-loop*, but you can measure the oracle's soundness *out-of-loop*. Make `evaluate_v2.py --all` the number you optimize during architecture development: when a rule is gate-accepted but scores poorly externally (as 8.3.5.1 did 0/4, 8.1.2.2 3/40), that is a **generic oracle/spec-generation defect** to fix in the prompt/fixture strategy — exactly how the distilled prompt rules were already derived. Systematize it (run it after every stored rule; track the trend) instead of doing it ad hoc.

**T3. Never discard accumulated work; stop the context balloon (fixes L3, L4).**
- Catch `BudgetExceeded` inside `agentic_draft_node`, `break`, and return the `best` candidate (elitism) instead of re-raising to null.
- Compact the loop context: keep only the *latest* module + *latest* scorecard; summarize/evict older `evaluate`/`inspect` turns so cumulative input stops growing linearly. This alone should let 24 turns fit inside 500 K.
- *Change: `agentic_draft.py` (exception handling + message pruning).*

### Tier 2 — Do these next (medium leverage)

**T4. Make fixture verdicts *derived*, not guessed — the real fix for L1, no labels needed.**
Today `expected_verdict` is a free LLM field. But for an injection fixture the verdict is often **computable** from what the operator did *and the numeric threshold in the spec card*: if the spec says "hook extension must be ≥ 264 mm" and the operator injects a bar with extension 200 mm, that fixture is `fail` **by construction** — and `measure.py` already independently re-measures the 200 mm. So: require conditions in the spec card to carry **machine-readable thresholds** (operator, quantity, comparator, value), have the operator inject *relative to the threshold* ("insert a bar 30% under `min_extension`"), and let `measure.py` **compute** the verdict from `measured_quantity vs threshold` rather than trusting the LLM's guess. This turns self-verification from "the perturbation happened" into "the perturbation *plus the rule arithmetic* yields this verdict" — a genuine internal oracle anchored to the spec's numbers, not the model's mood. It won't cover every rule (pure-geometry joints like 8.3.7.2 are harder), but it makes the numeric-threshold rules (most of 8.1.x / 8.3.5.x) soundly gradable. This is the highest-value structural change; scope it after Tier 1.

**T5. Use minimal synthetic bases for *all* fixture classes, including NA/unknown (fixes L5).**
Build the "no beams → not_applicable" fixture from a 3 KB synthetic scene with the elements omitted, not by deleting from a 56 MB model. Kills the giant-fixture cost, speeds `inspect`/`evaluate`, and removes the giant-fixture↔failure correlation. Add at least one *real-idiom* synthetic fail/pass fixture (IFC2X3 composite curve, mapped body, meters) so pass/fail is proven on production-like representation, not just IFC4/mm indexed scenes. *Change: `planner.py` base selection, one new synthetic scaffold variant.*

**T6. Trial a reasoning-capable spec model (fixes L6).**
Point `SPEC_MODEL` at gemini-2.5-pro or claude for the single spec call, with an enforceable thinking budget. Interpretation is the cognitive bottleneck and it's one cheap call per rule. Keep deepseek as fallback. Measure card quality on 8.3.7.2 / 8.3.4.2 (the arithmetic-heavy rules).

**T7. Port the missing doctrine (fixes L7).**
Add to `DRAFTER_SYSTEM_PROMPT`: level-by-level/storey coverage, exhaustive multi-violation discovery (don't stop at first), 1500 mm starter-bar extension for column-bar association, slab aspect-ratio one-way/two-way classification, and within-model mixed-unit detection. These are cheap prose additions lifted from `SKILL.md:24,27-41`.

### Tier 3 — Only if Tier 1–2 don't close the gap

**T8. Lightweight interpretation-confidence flag (mitigates L8).**
When a card carries many `proposed` conventions that its own fixtures can't discharge, flag it for a 2-minute human interpretation review *before* spending the draft budget — avoiding 8.3.7.2-style $0.28 null runs. This is not full HITL; it's a pre-flight triage. Sell the human-adjudication step as the compliance-audit feature it already is.

**T9. Remove the glm-5.2 fallback path or quarantine its output (mitigates L8).**
A fallback to a known-broken model that ships `except: pass` code is worse than failing cleanly. Either drop it or route its output through the same static gate before it can become a stored candidate (the AST gate should already catch `except: pass` — verify it runs on fallback drafts too).

### Explicitly NOT recommended (would be overkill / evidence against)
- Multi-drafter consensus, embedding retrieval (library of ~4), an LLM "card-doctor" replacing human adjudication — all already tried or rejected with evidence (`AGENTIC-LOOP-DESIGN.md:29-33`).
- Going back to requiring labeled IFC files to *drive* generation — the fixture innovation is worth keeping; only the *validation* anchor and the *interpretation* anchor need restoring.

---

## 4. The one-paragraph diagnosis

The IDE agent worked because it iterated, without limit, against **human-anchored truth** while a broad doctrine kept it out of IFC's traps. The app must give up that human anchor by design — new rules have no labels — so its whole reliability rests on two things being good enough to stand in for the anchor: the **interpretation** it derives from rule text, and the **synthetic oracle** it grades against. Today both are weak: interpretation is guessed by a no-reasoning model with no disambiguation channel that generalizes (L2/L6), and the oracle is the same model grading its own verdicts (L1). On top of that the loop **discards its own work at the budget ceiling** (L3/L4) and only ever proves pass/fail on toy scenes (L5). The result: easy unambiguous rules with clean synthetic fixtures succeed (8.3.5.1); hard or ambiguous ones certify the wrong interpretation or burn out with nothing to show. The fixes therefore aim to make interpretation and the synthetic oracle *sounder on their own terms* (T1, T4, T6) and stop wasting the iteration budget (T3, T5) — measured, out-of-loop, by `evaluate_v2.py` on the 19 held-out rules.

---

## 5. Suggested first commit (smallest change that should move the needle)

1. `agentic_draft.py`: catch `BudgetExceeded` → return the best candidate instead of `null`; prune stale loop turns so cumulative context stops growing (T3). *(Pure bug/efficiency fix — no methodology question.)*
2. `config.py`/`.env`: point `SPEC_MODEL` at a reasoning-capable model for the one spec call, and/or turn `SPEC_REASONING_EFFORT` up with an enforced cap (T6).
3. Optional, if you accept it as a valid input: add `interpretation_notes` to `rule.json` and feed it to the spec prompt (T1a).

Then run `python scripts/evaluate_v2.py --all` as the before/after number, and re-run generation on **8.3.7.2**, **8.1.6.5**, and **8.3.5.1** (regression). Labels stay strictly in `evaluate_v2.py`, never in the loop.

---

---

# Part II — The actual bottleneck under the full-autonomy constraint (research addendum, 2026-07-23)

**Constraint set (owner decision, final):** per rule, the agent gets `rule_text` + `explanation` + the unlabeled IFC corpus + **web access** (Tavily key already in `.env`, currently unused by any code). No human interpretation, no human adjudication, no labeled cases in the loop. The 19 labeled rules are dev-time eval only (`scripts/evaluate_v2.py`).

To find the *actual* bottleneck, all 19 rules were classified by where the knowledge needed to reproduce their labeled reasons actually lives, and the public availability of the missing knowledge was verified by fetching the official BNBC chapter text.

## II.1 Where the required knowledge lives (all 19 rules classified)

| Knowledge situation | Rules | Count |
|---|---|---|
| **Fully self-contained** — every number in the labeled reasons is derivable from the rule's own text+explanation | 8.1.2.1.A/B, 8.1.2.2, 8.1.6.2, 8.1.6.3, 8.1.6.5, 8.1.6.6, 8.3.10.5.A, 8.3.4.1.B.D, 8.3.5.1, 8.3.5.3.A, 8.3.5.4.A.D | **12** |
| **Needs cross-rule resolution, resolvable from the local rules DB** | 8.1.6.4 (→ 8.1.6.1 + 8.1.6.3 thresholds), 8.1.2.1.C (clause-selection convention) | **2** |
| **Reference cited but unchecked by labels** (resolver optional today) | 8.3.4.3 (→ 8.1.9.4(c), not local), 8.3.10.4 (part (a) moment strength) | **2** |
| **Genuinely needs EXTERNAL knowledge** | **8.3.7.2** — `ldh = fy·db/(5.4·√f'c)` (BNBC 8.2.10.1) appears in *no* local file (grep-verified: "5.4"/"ldh"/"fydb" occur only inside 8.3.7.2's own answer labels); its 12db hook extensions exist locally but in a *different* rule's text (8.1.2.1.B). **8.3.4.2** — the rule_text itself is **wrong vs the real code**: it drops the √ ("0.25(f'c/fy)·bw·d" instead of 0.25·√f'c/fy·bw·d), so a literal reading computes a ~5× wrong ρmin; correcting it requires external BNBC/ACI knowledge | **2** |
| **Needs a model-data convention supplying a number** | 8.1.6.1 (the "27 mm" = 1.33 × `MaxAggregateSize`=20 read from an IFC property) | **1** |

**Verified against the official code text** (fetched from law.resource.org): 8.3.7.2's official wording matches the repo's rule.json verbatim; the referenced 8.3.5.4 text is present in full; the adjacent 8.3.7.x provisions name 8.3.7.4/Sec 8.2 for anchorage — i.e., the knowledge chain the labels test is real BNBC structure, and it is retrievable. **Version trap (verified):** the free web copy is BNBC **2011/2012**; its 8.3.5.4(b) says "≤ ¼ minimum member dimension nor **100 mm**" while the local rules (and labels) follow BNBC **2020** with `s_o = 100+(350−hx)/3` capped 100–150 mm. Also PDF math extraction is lossy (formulas garble into glyph soup). ⇒ retrieval must prefer a **locally cached, curated BNBC-2020 chapter corpus**, with live web as fallback only.

## II.2 The bottleneck stack, ranked (what actually blocks full autonomy)

**B1 — Measurement-convention grounding (dominant; affects ALL 19 rules).**
The text/formula gap is small (17/19 closable locally), but *every* rule requires unstated IFC measurement conventions: bend-angle/extension extraction, stirrup-vs-longitudinal classification, effective-depth reconstruction, joint-face location, lap-splice detection, slab one/two-way classification, property locations (`MaxAggregateSize`, diameter-in-Name), and scope restrictions (8.1.6.1 checks beams only; 8.3.5.1's axial-force trigger is unknowable from IFC — labels assume it fires). **Every real failure to date was a convention failure, not a formula failure**: 8.3.5.1 external 0/4 (applicability + dims conventions), 8.1.2.2 3/40 (composite-curve idiom), 8.1.6.1 4 misses (diameter-in-Name), 8.3.7.2 (7 invented conventions incl. a fabricated 75 mm cover). This is exactly the layer the human adjudications were patching — so under full autonomy it must be **grounded empirically instead**: a convention is valid only if the data it reads exists in the real corpus (or it declares `on_missing`). The machinery half-exists (census/model-profile, `inspect` tool); what's missing is a **mechanical convention-validator**: for each proposed convention, probe the unlabeled corpus (does the attribute/pset/geometry it reads exist? on what fraction of elements?) and auto-reject/rewrite conventions that read data the corpus doesn't carry (the census already reveals e.g. that no corpus bar has `NominalDiameter`). That converts the recurring 2-minute human adjudication into a deterministic corpus check.

**B2 — Knowledge grounding (2–4 rules, incl. the current failure 8.3.7.2).**
Deterministic local reference-resolver (Sec X.Y → local rules DB) + curated BNBC-2020 corpus retrieval + bounded Tavily fallback. Also gives a cross-check channel for **wrong rule_text** (8.3.4.2's missing √): when a formula in rule_text disagrees with the retrieved official text, flag the discrepancy in the spec card rather than silently trusting either.

**B3 — Oracle verdict soundness (was L1; fix = derived verdicts, T4).**
Unchanged from Part I: verdicts computed from machine-readable thresholds + independent measurement, not authored by the LLM. Note B2 feeds B3: resolved references supply the *correct* thresholds to derive against.

**B4 — Loop mechanics (was L3/L4/L5; fixes T3/T5).**
Budget-discard bug, context balloon, giant fixtures. Pure engineering; no research question.

**B5 — Spec-stage cognition (was L6; fix T6).**
The interpretation stack above (B1+B2) raises the ceiling; a reasoning-capable spec model is still needed to use it well on geometry-heavy rules (joint identification in 8.3.7.2 remains genuinely hard even with all text resolved).

## II.3 Revised build order (full-autonomy path)

1. **T3** (budget bug + context pruning) — unblocks everything else; pure bug fix.
2. **B2**: local reference resolver (an afternoon; deterministic) + drop the curated BNBC-2020 chapter text into `data/` as retrieval corpus + wire Tavily as a bounded spec-stage tool (it's already paid for and unused).
3. **B1**: convention-validator — extend the existing census/model-profile into a mechanical pass over every proposed convention (`reads: attribute/pset/geometry` → corpus coverage % → accept / rewrite-to-fallback / on_missing). Plus the no-invented-constants prompt rule.
4. **T6**: reasoning-capable spec model trial (now measurable against a better-grounded spec stage).
5. **T4**: derived verdicts (biggest structural change; do once 1–4 stabilize).
6. Measure each step with `evaluate_v2.py --all`; regression set = 8.3.7.2 (knowledge+conventions), 8.1.6.5 (fixtures/oracle), 8.3.5.1 (no-regression).

**Prediction to falsify:** steps 1–3 alone should let 8.3.7.2 produce a real candidate (the spec card would contain the actual 8.3.5.4 spacing text + corpus-validated conventions instead of 7 invented ones), and should lift the 12 self-contained rules to near-IDE reliability since their only gaps are conventions (B1) and loop mechanics (B4).
