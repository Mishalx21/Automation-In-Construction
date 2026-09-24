"""
Build the Positive / Negative fixture corpus the v3/v4 harness grades against.

The harness reads `test_download/Archi_Test_cases/` and `test_download_struct/`
but neither ships in this repository: only the manifests do, and they point at
absolute paths on a machine nobody here has. This script regenerates the whole
corpus locally from the real models plus the rule library in
`ifc-fault-injector`, so `agentic_pipeline_v4/run_pipeline.py` can actually be
run against every accepted checker.

    python scripts/build_fixture_corpus.py --corpus both

What it produces, per corpus:

    Negative/<building>/<source>.ifc            the untouched model
    Positive/<building>/<building>_<RULE>_...   one rule injected
    Positive_Multi/<building>/..._MULTI_A1-A6   every applicable rule at once
    manifest.json / manifest_structural.json    what the harness reads

Every fixture is SELF-VERIFIED before it is written into the manifest: the
file is re-opened from disk, swept for dangling references, and the clause is
independently re-derived by `ifcfault.verify.checks`, which never imports the
rule library. A fixture that fails any of those is discarded and listed under
`rejected` with the reason, rather than quietly becoming a test case that
proves nothing.

The metadata each fixture carries is derived from the MUTATION RECORD alone -
never from what a checker says about the file afterwards. That direction
matters: the harness uses `global_id` / `target_storey` to ask "did the
checker point at the thing that actually changed?", and answering that
question with the checker's own output would make the test vacuous.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INJECTOR = REPO_ROOT.parent / "ifc-fault-injector"
DEFAULT_SOURCE_ROOT = REPO_ROOT.parents[1] / "models"

# --------------------------------------------------------------------------
# corpora
#
# The harness picks the corpus from the rule id (A* -> architectural,
# S* -> structural), so an architectural rule has to be injected into an
# architectural model and vice versa. Where a rule does not apply to any
# model in its own corpus it simply gets no positive fixture, and the
# harness reports it as ungradeable rather than passing it by default.
# --------------------------------------------------------------------------
ARCH_MODELS = [
    ("dental_clinic", "arc.ifc"),
    ("schependomlaan", "arc_design.ifc"),
    ("sixty5", "arc.ifc"),
    ("wbdg_office", "arc.ifc"),
    ("west_riverside_hospital", "arc.ifc"),
]
STRUCT_MODELS = [
    ("dental_clinic", "str.ifc"),
    ("schependomlaan", "str_engineer.ifc"),
    ("sixty5", "str.ifc"),
    ("wbdg_office", "str.ifc"),
    ("west_riverside_hospital", "str.ifc"),
]
ARCH_RULES = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10"]
STRUCT_RULES = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"]

CORPORA = {
    "arch": {
        "root": REPO_ROOT / "test_download" / "Archi_Test_cases",
        "manifest": REPO_ROOT / "test_download" / "Archi_Test_cases" / "manifest.json",
        "models": ARCH_MODELS,
        "rules": ARCH_RULES,
        "positive_key": "positive",
    },
    "struct": {
        "root": REPO_ROOT / "test_download_struct",
        "manifest": REPO_ROOT / "test_download_struct" / "manifest_structural.json",
        "models": STRUCT_MODELS,
        "rules": STRUCT_RULES,
        "positive_key": "single_error",
    },
}

# Rules whose mutation deletes elements. They go last in a multi fixture, so
# they cannot remove the element another rule was about to edit.
DELETION_RULES = {"S4", "S5", "S8", "S9"}

# Rules whose target_global_id is a storey rather than an element. The
# harness attributes these by storey name instead of by GUID.
STOREY_SCOPED = {"S5", "S8", "S9"}

# Rules whose independent re-derivation needs the untouched baseline as well
# as the fixture. A3 downgrades a fire rating, and "downgraded" only means
# anything against what the rating was before. The baseline is opened only
# for these, because holding a second copy of a 300 MB model in memory for
# every rule would be wasteful when only one rule reads it.
SOURCE_DEPENDENT_RULES = {"A3"}


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def build_meta(rule_id: str, mutation: dict) -> dict:
    """Harness metadata for one mutation, derived only from the record.

    Three shapes, because three kinds of clause:

    * storey-scoped (S5/S8/S9) - the violation belongs to a level, not an
      element, so the harness attributes it by `target_storey`. `element_type`
      is set to what was actually deleted, which is also what the harness's
      scope-guard recounts per storey to prove the deletion stayed local.
    * space-scoped (A8) - the clause governs the ROOM's opening ratio; the
      window is only the means. The record names the room, so the room is the
      element whose compliance changed.
    * everything else - the element the record targeted.
    """
    extra = mutation.get("extra", {})
    meta = {
        "rule": rule_id,
        "description": mutation["description"],
        "element_type": mutation["element_type"],
        "attribute": mutation["attribute"],
        "before": mutation["before"],
        "after": mutation["after"],
        "clause": mutation["clause"],
        "mutated_global_id": mutation["target_global_id"],
    }

    if rule_id in STOREY_SCOPED:
        meta["target_storey"] = extra.get("storey_name")
        deleted = extra.get("deleted_elements") or []
        types = [d.get("type") for d in deleted if d.get("type")]
        if types:
            # The dominant deleted type, so the scope-guard recount is about
            # the elements this mutation actually removed.
            meta["element_type"] = max(set(types), key=types.count)
        meta["deleted_count"] = (
            extra.get("deleted_count") or extra.get("deleted_wall_count") or len(deleted)
        )
    elif rule_id == "A4":
        meta["global_id"] = mutation["target_global_id"]
        meta["host_wall_global_id"] = extra.get("host_wall_global_id")
    elif rule_id == "A5":
        meta["global_id"] = mutation["target_global_id"]
        meta["severed_space_global_id"] = extra.get("severed_space_global_id")
        meta["remaining_space_global_id"] = extra.get("remaining_space_global_id")
        meta["action"] = extra.get("mechanism")
    elif rule_id == "A8":
        meta["global_id"] = extra.get("space_global_id")
        meta["space_name"] = extra.get("space_name")
        meta["required_percent"] = extra.get("required_percent")
        meta["space_floor_area_m2"] = extra.get("space_floor_area_m2")
    elif rule_id == "S4":
        # The deleted column cannot be reported; the column left hanging is.
        surviving = extra.get("surviving_column_global_id")
        meta["surviving_column_global_id"] = surviving
        # The harness looks for this spelling in GUID_KEY_CANDIDATES.
        meta["surviving_column_above_global_id"] = surviving
        meta["deleted_storey"] = extra.get("deleted_storey_name")
    else:
        meta["global_id"] = mutation["target_global_id"]

    if extra.get("pset_name"):
        meta["pset"] = extra["pset_name"]
    return {k: v for k, v in meta.items() if v is not None}


def verify_fixture(path: Path, rule_id: str, mutation: dict, checks, find_dangling,
                   source: Path | None = None):
    """Re-open the written file and prove the defect is really in it."""
    import ifcopenshell

    parsed = checks.check_parses_cleanly(path)
    if not parsed.passed:
        return False, f"file does not re-open: {parsed.message}"

    model = ifcopenshell.open(str(path))
    dangling = find_dangling(model)
    if dangling:
        return False, f"{len(dangling)} dangling reference(s) after the edit"

    clause_check = checks.CLAUSE_CHECKS.get(rule_id)
    if clause_check is None:
        return False, f"no independent re-derivation exists for {rule_id}"
    baseline = None
    if rule_id in SOURCE_DEPENDENT_RULES and source is not None and source.exists():
        baseline = ifcopenshell.open(str(source))
    try:
        verdict = clause_check(model, mutation, baseline)
    except Exception as exc:  # noqa: BLE001
        # Normal in a combined fixture: a later rule may have deleted the very
        # element an earlier one edited, so the earlier clause has nothing left
        # to stand on. That is a dropped case, not a crash.
        return False, f"re-derivation could not resolve its target ({exc!r})"
    if not verdict.passed:
        return False, f"clause not confirmed in the written file: {verdict.message}"
    return True, None


def verify_fixture_many(path: Path, records: list, checks, find_dangling,
                        source: Path | None = None):
    """Verify several clauses against one written file, opening it once.

    A combined fixture would otherwise be re-read per rule, which on a
    300 MB model costs more than generating it did.
    Returns (surviving records, [(rule_id, reason) for the rest]).
    """
    import ifcopenshell

    parsed = checks.check_parses_cleanly(path)
    if not parsed.passed:
        return [], [(r["rule"], f"file does not re-open: {parsed.message}") for r in records]

    model = ifcopenshell.open(str(path))
    dangling = find_dangling(model)
    if dangling:
        return [], [(r["rule"], f"{len(dangling)} dangling reference(s)") for r in records]

    baseline = None
    if any(r["rule"] in SOURCE_DEPENDENT_RULES for r in records) and source and source.exists():
        baseline = ifcopenshell.open(str(source))

    surviving, dropped = [], []
    for record in records:
        clause_check = checks.CLAUSE_CHECKS.get(record["rule"])
        if clause_check is None:
            dropped.append((record["rule"], "no independent re-derivation exists"))
            continue
        try:
            verdict = clause_check(model, record["_mutation"], baseline)
        except Exception as exc:  # noqa: BLE001
            # Normal here: a later rule may have deleted the element an
            # earlier one edited, so that clause has nothing left to stand on.
            dropped.append((record["rule"], f"target no longer resolves ({exc!r})"))
            continue
        if verdict.passed:
            surviving.append(record)
        else:
            dropped.append((record["rule"], verdict.message or "clause not confirmed"))
    return surviving, dropped


def inject_one(source: Path, rule_id: str, get_rule, best_target):
    """Open a fresh copy of the source and apply one rule to it."""
    import ifcopenshell

    module = get_rule(rule_id)
    model = ifcopenshell.open(str(source))
    applicability = module.applicable(model)
    if not applicability.ok:
        return None, None, applicability.reason
    targets = module.candidates(model)
    if not targets:
        return None, None, "applicable but produced no candidates"
    mutation = module.apply_violation(model, best_target(targets), {})
    return model, dataclasses.asdict(mutation), None


def build_model(corpus_name, corpus, building, source_name, rules, args, api):
    """Negative, one Positive per rule, and one Positive_Multi for a model."""
    get_rule, best_target, checks, find_dangling = api
    source = Path(args.source_root) / building / source_name
    if not source.exists():
        print(f"  !! source model missing: {source}")
        return None

    root = corpus["root"]
    neg_dir = root / "Negative" / building
    pos_dir = root / "Positive" / building
    multi_dir = root / "Positive_Multi" / building
    for d in (neg_dir, pos_dir, multi_dir):
        d.mkdir(parents=True, exist_ok=True)

    negative = neg_dir / source_name
    if args.force or not negative.exists():
        shutil.copy2(source, negative)
    print(f"  negative  {negative.relative_to(REPO_ROOT)}")

    entry = {
        "folder": building,
        "source_file": source_name,
        corpus["positive_key"]: [],
        "negative": source_name,
        "skipped_rules": [],
        "rejected": [],
    }

    for rule_id in rules:
        name = f"{building}_{rule_id}_{_slug(rule_id)}.ifc"
        started = time.time()
        try:
            model, mutation, reason = inject_one(source, rule_id, get_rule, best_target)
        except Exception as exc:  # noqa: BLE001
            entry["rejected"].append({"rule": rule_id, "reason": f"injector raised {exc!r}"})
            print(f"  {rule_id:<4} ERROR    {exc!r}")
            if args.traceback:
                traceback.print_exc()
            continue
        if model is None:
            entry["skipped_rules"].append(rule_id)
            print(f"  {rule_id:<4} skipped  {reason[:78]}")
            continue

        name = f"{building}_{rule_id}_{_slug(mutation['attribute'])[:40]}.ifc"
        out = pos_dir / name
        model.write(str(out))

        ok, why = verify_fixture(out, rule_id, mutation, checks, find_dangling, source)
        if not ok:
            out.unlink(missing_ok=True)
            entry["rejected"].append({"rule": rule_id, "reason": why})
            print(f"  {rule_id:<4} REJECTED {why[:78]}")
            continue

        entry[corpus["positive_key"]].append({
            "rule": rule_id,
            "path": name,
            "meta": build_meta(rule_id, mutation),
            "self_verified": True,
        })
        print(f"  {rule_id:<4} positive {name}  ({time.time() - started:.0f}s)")

    prune_orphans(pos_dir, {e["path"] for e in entry[corpus["positive_key"]]})

    applied_rules = [e["rule"] for e in entry[corpus["positive_key"]]]
    if len(applied_rules) >= 2 and not args.no_multi:
        multi = build_multi(source, applied_rules, building, multi_dir, api, args)
        if multi:
            entry["multi_error"] = multi
    prune_orphans(multi_dir, {entry["multi_error"]["path"]} if entry.get("multi_error") else set())

    return entry


def prune_orphans(directory: Path, keep: set[str]) -> None:
    """Delete fixtures in `directory` that this run did not produce.

    A rebuild can name a fixture differently - a rule that now applies where
    it did not before changes a combined fixture's name - and the harness
    finds architectural multi fixtures by globbing the directory rather than
    by reading the manifest. Left alone, the superseded file would keep being
    graded alongside its replacement.
    """
    for stale in sorted(directory.glob("*.ifc")):
        if stale.name not in keep:
            stale.unlink()
            print(f"  pruned   {stale.name} (superseded by this run)")


def build_multi(source, rule_ids, building, out_dir, api, args):
    """Every rule that worked on this model, applied to one file.

    Deletion rules go last: a rule that removes elements must not take away
    the element another rule was about to edit. Each rule re-derives its own
    candidates against the partially mutated model, so a target that an
    earlier edit invalidated is simply dropped.
    """
    import ifcopenshell

    get_rule, best_target, checks, find_dangling = api
    ordered = ([r for r in rule_ids if r not in DELETION_RULES]
               + [r for r in rule_ids if r in DELETION_RULES])

    model = ifcopenshell.open(str(source))
    applied = []
    for rule_id in ordered:
        module = get_rule(rule_id)
        try:
            if not module.applicable(model).ok:
                continue
            targets = module.candidates(model)
            if not targets:
                continue
            mutation = dataclasses.asdict(
                module.apply_violation(model, best_target(targets), {})
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  multi: {rule_id} dropped ({exc!r})")
            continue
        applied.append({"rule": rule_id, "meta": build_meta(rule_id, mutation),
                        "_mutation": mutation})

    if len(applied) < 2:
        return None

    ids = "-".join(a["rule"] for a in applied)
    name = f"{building}_MULTI_{ids}.ifc"
    out = out_dir / name
    model.write(str(out))

    # Every clause in the file has to still hold, or the combination broke
    # one of the defects it was meant to carry.
    surviving, dropped = verify_fixture_many(out, applied, checks, find_dangling, source)
    for rule_id, why in dropped:
        print(f"  multi: {rule_id} not confirmed in the combined file ({why[:60]})")
    if len(surviving) < 2:
        out.unlink(missing_ok=True)
        return None

    if len(surviving) != len(applied):
        # Rename so the filename lists only the rules actually carried: the
        # architectural manifest has no per-rule meta and the harness reads
        # that list straight off the name.
        out.unlink(missing_ok=True)
        ids = "-".join(a["rule"] for a in surviving)
        name = f"{building}_MULTI_{ids}.ifc"
        out = out_dir / name
        model.write(str(out))

    print(f"  multi    {name} carrying {ids}")
    for record in surviving:
        record.pop("_mutation", None)
    return {"path": name, "rules": [a["rule"] for a in surviving], "applied": surviving}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--corpus", choices=["arch", "struct", "both"], default="both")
    parser.add_argument("--models", help="comma-separated building folders to limit to")
    parser.add_argument("--rules", help="comma-separated rule ids to limit to")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--injector", default=str(DEFAULT_INJECTOR))
    parser.add_argument("--no-multi", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="re-copy negatives that already exist")
    parser.add_argument("--traceback", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.injector).resolve()))
    from ifcfault.library import get as get_rule
    from ifcfault.library.contract import best_target
    from ifcfault.library.edits import find_dangling_references
    from ifcfault.verify import checks
    api = (get_rule, best_target, checks, find_dangling_references)

    only_models = set(args.models.split(",")) if args.models else None
    only_rules = set(args.rules.split(",")) if args.rules else None

    names = ["arch", "struct"] if args.corpus == "both" else [args.corpus]
    for corpus_name in names:
        corpus = CORPORA[corpus_name]
        rules = [r for r in corpus["rules"] if not only_rules or r in only_rules]
        manifest_path = Path(corpus["manifest"])
        existing = {}
        if manifest_path.exists():
            try:
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing = {m["folder"]: m for m in previous.get("models", [])}
            except Exception:  # noqa: BLE001
                existing = {}

        models = []
        for building, source_name in corpus["models"]:
            if only_models and building not in only_models:
                # Keep what a previous run recorded for a building this run
                # is not touching, so a partial rebuild never truncates the
                # manifest.
                if building in existing:
                    models.append(existing[building])
                continue
            print(f"[{corpus_name}] {building}/{source_name}")
            entry = build_model(corpus_name, corpus, building, source_name, rules, args, api)
            if entry is not None:
                models.append(entry)

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "generated_by": "scripts/build_fixture_corpus.py",
            "source_root": str(Path(args.source_root).resolve()),
            "note": (
                "Every positive fixture was re-opened from disk, swept for dangling "
                "references, and had its clause independently re-derived by "
                "ifcfault.verify.checks before being listed here. Fixture metadata is "
                "derived from the mutation record only, never from a checker's output."
            ),
            "models": models,
        }, indent=1), encoding="utf-8")
        print(f"[{corpus_name}] manifest -> {manifest_path.relative_to(REPO_ROOT)}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
