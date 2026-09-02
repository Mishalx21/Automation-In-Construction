# rules/ — BNBC rule definitions and labeled test-case matrix

## Provenance

Every `rules/<rule_id>/rule.json` in this directory was copied verbatim on
2026-07-12 from the old repository:

    D:\Code-Agent\data-v2\Rules\<rule_id>\rule.json

19 rules total (BNBC 2020 Chapter 8):

    8.1.2.1.A  8.1.2.1.B  8.1.2.1.C  8.1.2.2
    8.1.6.1    8.1.6.2    8.1.6.3    8.1.6.4   8.1.6.5   8.1.6.6
    8.3.10.4   8.3.10.5.A 8.3.4.1.B.D 8.3.4.2  8.3.4.3
    8.3.5.1    8.3.5.3.A  8.3.5.4.A.D 8.3.7.2

Each `rule.json` contains:
- `title`, `rule_text`, `explanation` — the clause and its interpretation notes
- `test_cases` — the labeled matrix: `{file_name, expected_result: bool, label, reason?}`
  against the legacy synthetic IFC corpus.

## IFC files are NOT in this repo

The IFC models referenced by `test_cases` (e.g. `8.1.2.1.A_P1.ifc`) are large
and remain in the old repo. Paths are configured in `benchmark/data_paths.py`:

- `LEGACY_IFC_DIR` — default `D:\Code-Agent\data-v2\IFC-files`, override with
  the `BENCHMARK_IFC_DIR` environment variable.
- `REAL_IFC_DIR` — real-world models, default `<repo>/ifc-files/`, override
  with `BENCHMARK_REAL_IFC_DIR`.

## Label quality warning — read before scoring anything

The labels in `test_cases` are **known to be partially wrong** (see
`docs/DEEP-ANALYSIS-2026-07-23.md` and the recovered
adjudication docs in `benchmark/adjudication/recovered/`). Do not consume
`rule.json` test_cases directly; go through `benchmark/labels.py`, which
applies `benchmark/adjudication/label_overrides.json` and returns
`expected=None` for cases still awaiting human adjudication (excluded from
scoring, reported separately).

Note: some rule.json label errors documented in the recovered docs were
already fixed upstream in the old repo (the "Resolved" sections); the copies
here include those upstream fixes as of the copy date.

## Future layout (Phase 2)

`rules/<rule_id>/` will additionally hold `spec_card.yaml` (human-adjudicated
interpretation contract) and `checker.py` (accepted v2 checker emitting the
`CheckResultV2` schema from `bnbc/contracts.py`).
