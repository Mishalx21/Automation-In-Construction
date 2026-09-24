# The fixture corpus

`agentic_pipeline_v3/harness.py` and `agentic_pipeline_v4/run_pipeline.py` grade
a checker by running it against **fixtures**: copies of the real models with one
documented violation injected, paired with the untouched baseline. The harness
asks three things of every fixture, and a checker is accepted only when all
three hold for every one of them:

1. does the checker **see** the clause at all (does its source reach the IFC
   types the fixture's own record names)?
2. does it return **`fail`** on the positive?
3. does it point at the **right element** — the GUID, or the storey, that the
   mutation record says changed?

The Negative baselines are not pass/fail evidence on their own; they feed the
regression signature in `agentic_pipeline_v3/signatures/<RULE>.json`, which
warns when a checker's verdict on an untouched model changes between runs.

## The corpus is generated, not shipped

The IFC fixtures are ~6 GB and are not in version control. Only the manifests
are. Rebuild the whole thing with:

```bash
python scripts/build_fixture_corpus.py --corpus both
```

It reads the real models from `../../models` and the rule library from
`../ifc-fault-injector`, and writes:

```
test_download/Archi_Test_cases/     architectural (A1–A10)
test_download_struct/               structural   (S1–S10)
    Negative/<building>/            the untouched model
    Positive/<building>/            one rule injected
    Positive_Multi/<building>/      every applicable rule at once
    manifest.json / manifest_structural.json
```

Useful flags: `--corpus arch|struct|both`, `--models dental_clinic,sixty5`,
`--rules A6,S8`. A partial run keeps the manifest entries for buildings it did
not touch, so the corpus can be rebuilt a model at a time — `sixty5/arc.ifc` is
327 MB and takes roughly two minutes per rule.

## Every fixture is self-verified before it is listed

A file that merely *claims* to carry a defect proves nothing. Before a fixture
enters the manifest it is re-opened from disk and must:

- parse cleanly;
- contain no dangling references (the edit did not orphan an entity);
- have its clause **independently re-derived** by `ifcfault.verify.checks`,
  which never imports the rule library and re-measures the quantity by its own
  route.

Anything that fails is deleted and recorded under `rejected` in the manifest
with the reason, so a gap in the corpus is visible rather than silent.

## Where the metadata comes from — and where it must not

Each fixture's `meta` is built **only from the mutation record**: what was
changed, on which element, from what to what. It is never derived from what a
checker says about the file afterwards. That direction is the whole point: the
harness uses `global_id` / `target_storey` to ask "did the checker point at the
thing that actually changed?", and answering that question with the checker's
own output would make the test vacuous.

Three shapes of `meta`, because there are three kinds of clause:

| Shape | Rules | Attribution key |
|---|---|---|
| element-scoped | most | `global_id` — the element the record targeted |
| storey-scoped | S5, S8, S9 | `target_storey` — the violation belongs to a level, not an element |
| space-scoped | A8 | `global_id` — the ROOM whose opening ratio changed; the window is only the means |

S4 is a fourth case: the column it deletes cannot be reported, so the column
left hanging above is the one attributed, under the
`surviving_column_above_global_id` key the harness looks for.

For storey-scoped rules `element_type` is set to what was actually deleted
(not `IfcBuildingStorey`), because the harness's scope-guard recounts that type
per storey to prove the deletion stayed local instead of wiping the building.

## Multi-error fixtures

`Positive_Multi/` carries one file per building with every applicable rule
injected at once. Deletion rules (S4, S5, S8, S9) are applied last so they
cannot remove the element another rule was about to edit, and each rule
re-derives its candidates against the partially mutated model.

A clause that no longer holds in the combined file is dropped, and the filename
lists only the rules the file actually carries — the architectural manifest has
no per-rule meta for these, and the harness reads the rule list straight off the
name.

Note that a combined fixture is weaker evidence than a single one: with no
per-rule meta, an architectural multi case can only be judged at verdict level,
which the harness labels `verdict_only(filename-only meta)` rather than quietly
treating it as a GUID-level match.

## What an ACCEPTED verdict does and does not mean

It means: the checker reached the clause, returned `fail` on every positive
fixture, and named the element or storey the mutation record says changed.

It does **not** mean the checker implements BNBC correctly. The fixture is
ground truth only for "a defect of this shape is present" — and for rules
A6–A10 and S6–S10 the checker, the injector and the independent re-derivation
were all written by the same author in the same sitting. If all three share a
misreading of a clause, they will agree with each other and the harness will
accept the result.

The corpus is therefore strong evidence of **detection** (the checker is not
blind, not silently skipping, and points at the right element) and weak
evidence of **interpretation**. Interpretation is what the spec-card and
conformance stages of the full agent pipeline are for, and what a human review
of each checker's stated conventions is for. Those conventions are written at
the top of every checker precisely so they can be argued with.

The older rules A1–A5 and S1–S5 do not have this problem — their checkers and
injectors came from different efforts — and the harness duly found real
disagreements between them.
