# Paper Outline — working title:
# "Fixture-Injected Verification: Reliable LLM Code Generation for Building-Code Compliance Checking Without Labeled Data"

Target: ICSE/FSE/ASE cycle (primary), NeurIPS D&B or ICLR (alternative framing).
Verify CFP dates ~4 weeks before intended submission.

## Contributions
1. **FIV (Fixture-Injected Verification)** — a verification method for
   LLM-generated checking code when no labeled data exists: deterministic,
   self-verifying perturbation operators inject known violations (and known
   data-degradations) into real IFC models, manufacturing an executable
   oracle. Generalizes to any "generate code over structured data with no
   labels" setting.
2. **Four-valued verdict semantics** (pass / fail / unknown / not_applicable)
   with auto-derived data-prerequisite manifests (IDS-aligned) — first
   LLM-era treatment of the missing-data problem in automated compliance
   checking; structurally eliminates vacuous passes (checked_summary
   accounting).
3. **BNBC-Check benchmark** — the first executable
   (clause, spec card, IFC models incl. degraded variants, adjudicated
   verdicts, violation locations) benchmark for compliance-checking code
   generation; includes a documented label-adjudication protocol over an
   existing noisy dataset.
4. **A cost-controlled system + failure study** — v1 (LLM-as-judge pipeline)
   forensics as motivation (94–99% verifier token share; discarded-verdict
   loop; vacuous acceptance), v2 architecture, and evaluation with cost
   Pareto, pass^k, and per-component ablations.

## Section sketch
1. Introduction — ACC rule-interpretation bottleneck; LLM codegen promise;
   the oracle problem in this domain.
2. Motivating failure study — v1 architecture + forensics (the 12.16M-token
   run; vacuous passes; measured verifier pathologies). MAST vocabulary.
3. FIV method — operators, self-verification, fixture planning from spec
   cards, acceptance gate; consensus layer; demoted conformance review;
   adjudication protocol (one-time human input per rule).
4. Verdict semantics + prerequisites manifests.
5. BNBC-Check benchmark — construction, label adjudication, splits
   (dev/val/held-out; held-out untouched during development).
6. Evaluation —
   RQ1 correctness vs baselines (single-shot, retry, majority-of-5, v1);
   RQ2 cost (tokens/$ per rule, Pareto);
   RQ3 reliability (pass^3);
   RQ4 ablations (−fixtures, K=1, −spec card, −retrieval, single-family);
   RQ5 generalization (held-out rules incl. other BNBC chapters).
7. Threats — single-jurisdiction scope, fixture representativeness,
   adjudication subjectivity (mitigate: publish adjudication docs),
   model contamination.
8. Related work — ACC (Eastman 2009; RASE; LLM-ACC 2023–26; LLM-FuncMapper;
   SGR-BIM; BNBC/Grasshopper ITcon paper), oracle problem (Barr 2015;
   metamorphic testing; mutation testing; CodeT/AlphaCode), agent
   architectures (Agentless; AlphaCodium; MAST; AI Agents That Matter).

## Headline experiments to run
- v2 gate REJECTS both v1-accepted broken checkers (8.1.2.1.A/B) — table 1.
- v2 fixes all three rules at ≤1/10 of v1's 8.1.2.1.B token cost.
- Held-out rule generalization with zero scaffold iteration.

## Artifact plan
Open-source: framework (this repo), BNBC-Check, adjudication docs, fixture
manifests. Report: per-run traces, seeds, model versions, prices.
