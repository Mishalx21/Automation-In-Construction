"""
Version 3 test harness -- fixes the five limitations found in
agentic_pipeline_v2 (see chat record / README_V3.md):

  V2 limitation                                   V3 fix
  ------------------------------------------------------------------------
  1. reachability = entity type only               STEP 1: also extracts
     (blind to relationship/attribute changes,             relationship/attribute
     missed A5)                                            identifiers from the
                                                            fixture's own meta text
  2. no fixture self-consistency check              STEP 0: independent
     (S5's over-deletion silently blamed                    storey-wise scope-guard
     on the checker)                                        recount; classifies
                                                            FIXTURE_FAULT separately
                                                            from CODE_FAULT
  3. silent downgrade to verdict-only when no        STEP 2: explicit
     GUID exists (false-confirmation risk)                  attribution_method field;
                                                            storey-name fallback signal
                                                            instead of a bare pass
  4. Positive_Multi never tested                    STEP 3: multi-error
                                                            fixtures included
                                                            (full meta for structural
                                                            rules; filename-only,
                                                            verdict-level for
                                                            architectural rules, and
                                                            explicitly labeled as such)
  5. no regression protection                       STEP 5: violation-count
                                                            signature recorded per
                                                            real Negative baseline;
                                                            future runs warn on drift

Step 4 (autonomous multi-round draft loop) is implemented in loop.py as a
pluggable driver -- this module only provides the mechanical judge it
calls each round.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCH_MANIFEST = ROOT / "test_download" / "Archi_Test_cases" / "manifest.json"
STRUCT_MANIFEST = ROOT / "test_download_struct" / "manifest_structural.json"
SIGNATURE_DIR = Path(__file__).resolve().parent / "signatures"

sys.path.insert(0, str(ROOT))
from ifc_helpers.helpers import element_storey  # noqa: E402

GUID_KEY_CANDIDATES = [
    "global_id",
    "surviving_column_above_global_id",
]

# words that should never be treated as a "referenced IFC type" even though
# they match the Ifc[A-Z]... pattern textually inside meta descriptions
_IFC_TYPE_RE = re.compile(r"\bIfc[A-Z][A-Za-z0-9]+\b")


@dataclass
class FixtureCase:
    building: str
    category: str            # "positive" | "negative" | "multi"
    ifc_path: Path
    rule_id: str
    meta: dict[str, Any] | None = None
    meta_confidence: str = "full"   # "full" | "filename_only" (arch multi has no per-rule meta)
    negative_path: Path | None = None  # paired baseline, used by the scope-guard


@dataclass
class FixtureResult:
    case: FixtureCase
    verdict: str
    fixture_fault: bool
    fixture_fault_note: str | None
    reachable: bool
    missing_identifiers: list[str]
    attribution_method: str    # "guid" | "storey" | "verdict_only" | "n/a"
    attribution_ok: bool | None
    ok: bool
    diagnostic: str | None = None


@dataclass
class RuleReport:
    rule_id: str
    checker_path: Path
    results: list[FixtureResult] = field(default_factory=list)

    @property
    def testable(self) -> list[FixtureResult]:
        """Positive/multi cases whose fixture itself was verified sound."""
        return [r for r in self.results if r.case.category != "negative" and not r.fixture_fault]

    @property
    def fixture_faults(self) -> list[FixtureResult]:
        return [r for r in self.results if r.fixture_fault]

    @property
    def accepted(self) -> bool:
        t = self.testable
        return bool(t) and all(r.ok for r in t)

    def feedback_text(self) -> str:
        lines = []
        for r in self.fixture_faults:
            lines.append(f"- FIXTURE FAULT (not a code issue): {r.case.building}/{r.case.ifc_path.name}")
            lines.append(f"    {r.fixture_fault_note}")
        for r in self.testable:
            if r.ok:
                continue
            lines.append(f"- CODE FAULT: {r.case.building}/{r.case.ifc_path.name} ({r.case.category})")
            if r.diagnostic:
                lines.append(f"    {r.diagnostic}")
        return "\n".join(lines) if lines else "(no failures)"


# ---------------------------------------------------------------------------
# STEP 0 -- fixture self-consistency / scope-guard
# ---------------------------------------------------------------------------
def scope_guard_check(meta: dict | None, fixture_path: Path, negative_path: Path | None) -> tuple[bool, str | None]:
    """Independently verify a deletion-style fixture stayed inside its
    declared scope (e.g. 'delete walls on storey X' shouldn't delete walls
    on every storey). Returns (is_valid, note)."""
    if not meta or "target_storey" not in meta or negative_path is None or not negative_path.exists():
        return True, None

    entity_type = meta.get("element_type", "IfcWall")
    target_storey = meta["target_storey"]

    import ifcopenshell

    def counts_by_storey(path: Path) -> dict[str, int]:
        model = ifcopenshell.open(str(path))
        counts: dict[str, int] = {}
        for el in model.by_type(entity_type):
            storey = element_storey(el)
            counts[storey] = counts.get(storey, 0) + 1
        return counts

    baseline_counts = counts_by_storey(negative_path)
    fixture_counts = counts_by_storey(fixture_path)

    other_storeys_with_elements = {
        s: c for s, c in baseline_counts.items() if s != target_storey and c > 0
    }
    if not other_storeys_with_elements:
        return True, None  # nothing to compare against; can't detect over-deletion this way

    wiped_elsewhere = [
        s for s, baseline_c in other_storeys_with_elements.items()
        if fixture_counts.get(s, 0) == 0
    ]
    if len(wiped_elsewhere) == len(other_storeys_with_elements):
        return False, (
            f"scope-guard failed: fixture meta declares the {entity_type} deletion was "
            f"scoped to storey '{target_storey}', but ALL other storeys that had "
            f"{entity_type} in the baseline ({sorted(other_storeys_with_elements)}) now have "
            f"zero in the fixture too -- the fixture deleted building-wide, not just the "
            f"target storey. This is a test-data generation fault, not a checker bug."
        )
    return True, None


# ---------------------------------------------------------------------------
# STEP 1 -- broadened reachability (entity type + relationship/attribute text)
# ---------------------------------------------------------------------------
def _referenced_ifc_identifiers(meta: dict | None) -> list[str]:
    if not meta:
        return []
    text = " ".join(str(meta.get(k, "")) for k in ("description", "action", "attribute", "pset"))
    return sorted(set(_IFC_TYPE_RE.findall(text)))


# IFC4 deprecated several "StandardCase" subtypes in favour of the plain
# supertype; ifcopenshell's by_type() already includes subtypes at runtime
# (by_type("IfcWall") reaches IfcWallStandardCase instances), so a checker
# that queries the plain type genuinely CAN see the subtype -- these are
# alternatives satisfying the same requirement (OR), never two separate
# requirements (AND). Mixing that up caused a false UNREACHABLE flag on
# every IfcWallStandardCase fixture even when the checker correctly used
# by_type("IfcWall").
_SUBTYPE_ALIASES = {
    "IfcWallStandardCase": "IfcWall",
    "IfcSlabStandardCase": "IfcSlab",
    "IfcColumnStandardCase": "IfcColumn",
    "IfcBeamStandardCase": "IfcBeam",
    "IfcMemberStandardCase": "IfcMember",
    "IfcPlateStandardCase": "IfcPlate",
}


def reachability_check(checker_source: str, meta: dict | None) -> tuple[bool, list[str]]:
    entity_type = (meta or {}).get("element_type")
    alias_groups: list[list[str]] = []
    if entity_type:
        group = [entity_type]
        if entity_type in _SUBTYPE_ALIASES:
            group.append(_SUBTYPE_ALIASES[entity_type])
        alias_groups.append(group)
    for ident in _referenced_ifc_identifiers(meta):
        if ident != entity_type:
            alias_groups.append([ident])

    missing = []
    for group in alias_groups:
        # broad check: does the checker source mention ANY alternative in
        # this group AT ALL (by_type call, inverse-attribute traversal,
        # anything) -- deliberately more permissive than v2's by_type-only
        # regex so it also catches relationship types like
        # IfcRelSpaceBoundary that a checker might reach via .BoundedBy
        # rather than by_type().
        if not any(alt in checker_source for alt in group):
            missing.append(group[0])
    return (len(missing) == 0), missing


# ---------------------------------------------------------------------------
# Manifest loading (single + multi)
# ---------------------------------------------------------------------------
def _arch_single(rule_id: str) -> list[FixtureCase]:
    if not ARCH_MANIFEST.exists():
        return []
    data = json.loads(ARCH_MANIFEST.read_text(encoding="utf-8"))
    cases = []
    for m in data["models"]:
        folder = m["folder"]
        neg_path = ROOT / "test_download" / "Archi_Test_cases" / "Negative" / folder / Path(m["negative"]).name
        for e in m.get("positive", []):
            if e.get("rule") != rule_id:
                continue
            p = ROOT / "test_download" / "Archi_Test_cases" / "Positive" / folder / Path(e["path"]).name
            if p.exists():
                cases.append(FixtureCase(folder, "positive", p, rule_id, e.get("meta"),
                                          negative_path=neg_path if neg_path.exists() else None))
        if neg_path.exists():
            cases.append(FixtureCase(folder, "negative", neg_path, rule_id))
    return cases


def _struct_single(rule_id: str) -> list[FixtureCase]:
    if not STRUCT_MANIFEST.exists():
        return []
    data = json.loads(STRUCT_MANIFEST.read_text(encoding="utf-8"))
    cases = []
    for m in data["models"]:
        folder = m["folder"]
        neg_path = ROOT / "test_download_struct" / "Negative" / folder / Path(m["negative"]).name
        for e in m.get("single_error", []):
            if e.get("rule") != rule_id:
                continue
            p = ROOT / "test_download_struct" / "Positive" / folder / Path(e["path"]).name
            if p.exists():
                cases.append(FixtureCase(folder, "positive", p, rule_id, e.get("meta"),
                                          negative_path=neg_path if neg_path.exists() else None))
        if neg_path.exists():
            cases.append(FixtureCase(folder, "negative", neg_path, rule_id))
    return cases


def _struct_multi(rule_id: str) -> list[FixtureCase]:
    if not STRUCT_MANIFEST.exists():
        return []
    data = json.loads(STRUCT_MANIFEST.read_text(encoding="utf-8"))
    cases = []
    for m in data["models"]:
        folder = m["folder"]
        me = m.get("multi_error")
        if not me or rule_id not in me.get("rules", []):
            continue
        p = ROOT / "test_download_struct" / "Positive_Multi" / folder / Path(me["path"]).name
        if not p.exists():
            continue
        meta = None
        for applied in me.get("applied", []):
            if applied.get("rule") == rule_id:
                meta = applied.get("meta")
        neg_path = ROOT / "test_download_struct" / "Negative" / folder / Path(m["negative"]).name
        cases.append(FixtureCase(folder, "multi", p, rule_id, meta,
                                  negative_path=neg_path if neg_path.exists() else None))
    return cases


def _arch_multi(rule_id: str) -> list[FixtureCase]:
    """Architectural manifest carries no per-rule meta for Positive_Multi,
    so this is filename-only and verdict-level (no GUID/storey attribution
    possible). meta_confidence is marked accordingly, and the report must
    treat these as weaker evidence than the fully-attributed cases."""
    d = ROOT / "test_download" / "Archi_Test_cases" / "Positive_Multi"
    if not d.exists():
        return []
    cases = []
    for p in d.rglob("*.ifc"):
        m = re.search(r"_MULTI_([A-Z0-9-]+)\.ifc$", p.name)
        if not m or rule_id not in re.findall(r"[AS]\d", m.group(1)):
            continue
        folder = p.parent.name
        cases.append(FixtureCase(folder, "multi", p, rule_id, None, meta_confidence="filename_only"))
    return cases


def load_fixtures(rule_id: str, include_multi: bool = True) -> list[FixtureCase]:
    if rule_id.startswith("A"):
        cases = _arch_single(rule_id)
        if include_multi:
            cases += _arch_multi(rule_id)
    elif rule_id.startswith("S"):
        cases = _struct_single(rule_id)
        if include_multi:
            cases += _struct_multi(rule_id)
    else:
        cases = _arch_single(rule_id) + _struct_single(rule_id)
    seen, out = set(), []
    for c in cases:
        key = (c.building, c.category, str(c.ifc_path))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _load_checker_module(checker_path: Path):
    spec = importlib.util.spec_from_file_location(f"candidate_{checker_path.stem}", checker_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _violations_text(result: dict) -> str:
    return json.dumps(result.get("violations", []))


def _violation_storeys(result: dict) -> set[str]:
    storeys = set()
    for v in result.get("violations", []):
        for loc in v.get("locations", []):
            if loc.get("storey"):
                storeys.add(loc["storey"])
    return storeys


def _extract_target_guid(meta: dict | None) -> str | None:
    if not meta:
        return None
    for key in GUID_KEY_CANDIDATES:
        if meta.get(key):
            return meta[key]
    return None


# ---------------------------------------------------------------------------
# STEP 2 -- robust attribution (guid -> storey -> verdict_only, transparently)
# ---------------------------------------------------------------------------
def attribute(result: dict, meta: dict | None) -> tuple[str, bool | None]:
    guid = _extract_target_guid(meta)
    if guid:
        return "guid", guid in _violations_text(result)

    target_storey = (meta or {}).get("target_storey") or (meta or {}).get("deleted_storey")
    if target_storey:
        return "storey", target_storey in _violation_storeys(result)

    return "verdict_only", None


def run_case(mod, case: FixtureCase, checker_source: str) -> FixtureResult:
    import ifcopenshell

    # STEP 0
    fixture_ok, fixture_note = scope_guard_check(case.meta, case.ifc_path, case.negative_path)
    if not fixture_ok:
        return FixtureResult(
            case, verdict="N/A", fixture_fault=True, fixture_fault_note=fixture_note,
            reachable=True, missing_identifiers=[], attribution_method="n/a",
            attribution_ok=None, ok=False,
        )

    # STEP 1
    reachable, missing = reachability_check(checker_source, case.meta)

    model = ifcopenshell.open(str(case.ifc_path))
    try:
        result = mod.check_rule(model)
    except Exception as exc:  # noqa: BLE001
        return FixtureResult(
            case, verdict="ERROR", fixture_fault=False, fixture_fault_note=None,
            reachable=reachable, missing_identifiers=missing, attribution_method="n/a",
            attribution_ok=None, ok=False, diagnostic=f"checker raised an exception: {exc!r}",
        )

    verdict = result.get("verdict", "unknown")

    if case.category == "negative":
        return FixtureResult(
            case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
            reachable=reachable, missing_identifiers=missing, attribution_method="n/a",
            attribution_ok=None, ok=True,
        )

    if not reachable:
        return FixtureResult(
            case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
            reachable=False, missing_identifiers=missing, attribution_method="n/a",
            attribution_ok=None, ok=False,
            diagnostic=(
                f"UNREACHABLE: checker source never references {missing} -- the fixture's "
                f"own description/meta names these IFC types/relationships but the checker "
                f"code doesn't mention them anywhere. meta={json.dumps(case.meta)[:300]}"
            ),
        )

    if verdict != "fail":
        return FixtureResult(
            case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
            reachable=reachable, missing_identifiers=missing, attribution_method="n/a",
            attribution_ok=None, ok=False,
            diagnostic=f"WRONG VERDICT: expected 'fail', got '{verdict}'. meta={json.dumps(case.meta)[:300]}",
        )

    # STEP 2
    method, attribution_ok = attribute(result, case.meta)
    if case.meta_confidence == "filename_only":
        # architectural multi fixtures have no per-rule meta to attribute
        # against; verdict=fail is the strongest evidence available, but
        # we say so explicitly rather than silently calling it equivalent
        # to a GUID-level match.
        return FixtureResult(
            case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
            reachable=reachable, missing_identifiers=missing, attribution_method="verdict_only(filename-only meta)",
            attribution_ok=None, ok=True,
        )

    if attribution_ok is False:
        return FixtureResult(
            case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
            reachable=reachable, missing_identifiers=missing, attribution_method=method,
            attribution_ok=False, ok=False,
            diagnostic=(
                f"FALSE-CONFIRMATION ({method}): verdict is 'fail' but the expected "
                f"{method} signal from the fixture's own meta never appears in the "
                f"violations. meta={json.dumps(case.meta)[:300]}"
            ),
        )

    return FixtureResult(
        case, verdict=verdict, fixture_fault=False, fixture_fault_note=None,
        reachable=reachable, missing_identifiers=missing, attribution_method=method,
        attribution_ok=attribution_ok, ok=True,
    )


def run_rule(rule_id: str, checker_path: Path, include_multi: bool = True) -> RuleReport:
    checker_source = checker_path.read_text(encoding="utf-8")
    mod = _load_checker_module(checker_path)
    cases = load_fixtures(rule_id, include_multi=include_multi)
    report = RuleReport(rule_id=rule_id, checker_path=checker_path)
    for case in cases:
        report.results.append(run_case(mod, case, checker_source))
    return report


# ---------------------------------------------------------------------------
# STEP 5 -- regression signature
# ---------------------------------------------------------------------------
def _signature_path(rule_id: str) -> Path:
    return SIGNATURE_DIR / f"{rule_id}.json"


def check_and_update_signature(report: RuleReport) -> list[str]:
    """Compare this run's Negative-baseline violation counts against the
    last accepted signature; return a list of drift warnings (empty if
    none, or if this is the first time this rule has been signed)."""
    neg_results = [r for r in report.results if r.case.category == "negative"]
    current = {r.case.building: r.verdict for r in neg_results}

    sig_path = _signature_path(report.rule_id)
    warnings: list[str] = []
    if sig_path.exists():
        previous = json.loads(sig_path.read_text(encoding="utf-8"))
        for building, verdict in current.items():
            prev_verdict = previous.get(building)
            if prev_verdict is not None and prev_verdict != verdict:
                warnings.append(
                    f"REGRESSION WARNING: {building} negative-baseline verdict changed "
                    f"from '{prev_verdict}' to '{verdict}' since the last accepted signature -- "
                    f"review before trusting this as a pure improvement."
                )

    if report.accepted:
        SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)
        sig_path.write_text(json.dumps(current, indent=2), encoding="utf-8")

    return warnings


def print_report(report: RuleReport) -> None:
    print(f"=== Rule {report.rule_id} (v3 harness) -- {report.checker_path} ===")
    for r in report.results:
        if r.fixture_fault:
            print(f"[FIXTURE FAULT] {r.case.category:8} {r.case.building:26} -- {r.fixture_fault_note}")
            continue
        mark = "OK  " if r.ok else "FAIL"
        print(f"[{mark}] {r.case.category:8} {r.case.building:26} verdict={r.verdict:14} "
              f"reachable={r.reachable} attribution={r.attribution_method}({r.attribution_ok})")
    print()

    if report.fixture_faults:
        print(f"{len(report.fixture_faults)} fixture(s) flagged as FIXTURE FAULT (excluded from verdict):")
        for r in report.fixture_faults:
            print(f"  - {r.case.building}/{r.case.ifc_path.name}")
        print()

    warnings = check_and_update_signature(report)
    for w in warnings:
        print(w)

    if report.accepted:
        print("VERDICT: ACCEPTED (every testable fixture attributes correctly)")
    else:
        print("VERDICT: REJECTED")
        print("FEEDBACK:")
        print(report.feedback_text())
