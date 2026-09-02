"""
Mechanical test harness for the agentic checker-generation loop.

Given a rule id (e.g. "A2", "S1") and a candidate checker file (must expose
check_rule(model) -> dict, same contract as every rule-*_v1 checker), this
locates that rule's Positive/Negative fixtures from the existing manifests,
runs the checker against every one, and produces a structured, MECHANICAL
diagnosis (no LLM judgment anywhere in this file) of exactly why a fixture
was missed:

  - reachability: does the checker's source even query the IFC entity type
    the fixture perturbed? (static text search over the checker's own code)
  - attribution: does the fixture's specific injected/affected element GUID
    appear among the checker's reported violations?
  - verdict: plain fail/not_applicable/pass/unknown match against what a
    Positive fixture requires (fail).

This is Stage 3 + Stage 4 from the redesigned pipeline discussed in-session:
a checker is only "correct" on a fixture if it (a) can reach the perturbed
entity type at all, and (b) names that exact element in its violations.
"""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCH_MANIFEST = ROOT / "test_download" / "Archi_Test_cases" / "manifest.json"
STRUCT_MANIFEST = ROOT / "test_download_struct" / "manifest_structural.json"

# candidate keys (in priority order) that might hold the GUID a checker
# should end up naming in its violations for a given rule's injection
GUID_KEY_CANDIDATES = [
    "global_id",
    "surviving_column_above_global_id",  # S4: the element that SHOULD get flagged
    "deleted_global_id",                 # S4: the element that was removed (won't appear)
]


@dataclass
class FixtureCase:
    building: str
    category: str          # "positive" | "negative"
    ifc_path: Path
    rule_id: str
    meta: dict[str, Any] | None = None


@dataclass
class FixtureResult:
    case: FixtureCase
    verdict: str
    reachable: bool
    reachability_note: str
    target_guid: str | None
    target_found: bool | None   # None = not applicable (e.g. negative fixture, or no usable guid)
    ok: bool
    diagnostic: str | None = None


@dataclass
class RuleReport:
    rule_id: str
    checker_path: Path
    results: list[FixtureResult] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        pos = [r for r in self.results if r.case.category == "positive"]
        return bool(pos) and all(r.ok for r in pos)

    def feedback_text(self) -> str:
        """Structured, agent-readable feedback for every failing case."""
        lines = []
        for r in self.results:
            if r.ok:
                continue
            lines.append(
                f"- FIXTURE {r.case.building}/{r.case.ifc_path.name} "
                f"({r.case.category}) -> FAILED"
            )
            if r.diagnostic:
                lines.append(f"    {r.diagnostic}")
        return "\n".join(lines) if lines else "(no failures)"


def _load_arch_entries(rule_id: str) -> list[FixtureCase]:
    if not ARCH_MANIFEST.exists():
        return []
    data = json.loads(ARCH_MANIFEST.read_text(encoding="utf-8"))
    cases: list[FixtureCase] = []
    for m in data["models"]:
        folder = m["folder"]
        neg_path = ROOT / "test_download" / "Archi_Test_cases" / "Negative" / folder / Path(m["negative"]).name
        for e in m.get("positive", []):
            if e.get("rule") != rule_id:
                continue
            pos_path = ROOT / "test_download" / "Archi_Test_cases" / "Positive" / folder / Path(e["path"]).name
            if pos_path.exists():
                cases.append(FixtureCase(folder, "positive", pos_path, rule_id, e.get("meta")))
        if neg_path.exists():
            cases.append(FixtureCase(folder, "negative", neg_path, rule_id, None))
    return cases


def _load_struct_entries(rule_id: str) -> list[FixtureCase]:
    if not STRUCT_MANIFEST.exists():
        return []
    data = json.loads(STRUCT_MANIFEST.read_text(encoding="utf-8"))
    cases: list[FixtureCase] = []
    for m in data["models"]:
        folder = m["folder"]
        neg_path = ROOT / "test_download_struct" / "Negative" / folder / Path(m["negative"]).name
        for e in m.get("single_error", []):
            if e.get("rule") != rule_id:
                continue
            pos_path = ROOT / "test_download_struct" / "Positive" / folder / Path(e["path"]).name
            if pos_path.exists():
                cases.append(FixtureCase(folder, "positive", pos_path, rule_id, e.get("meta")))
        if neg_path.exists():
            cases.append(FixtureCase(folder, "negative", neg_path, rule_id, None))
    return cases


def load_fixtures(rule_id: str) -> list[FixtureCase]:
    # rule ids are prefixed A (architectural) or S (structural) -- only the
    # matching manifest's negatives are relevant, avoiding cross-manifest
    # noise (e.g. structural schependomlaan has 3 unrelated negative files).
    if rule_id.startswith("A"):
        cases = _load_arch_entries(rule_id)
    elif rule_id.startswith("S"):
        cases = _load_struct_entries(rule_id)
    else:
        cases = _load_arch_entries(rule_id) + _load_struct_entries(rule_id)
    # de-dup negatives (same building can appear once per rule already, but be safe)
    seen = set()
    out = []
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


def check_reachability(checker_source: str, entity_type: str | None) -> tuple[bool, str]:
    if not entity_type:
        return True, "no entity_type declared in fixture meta; skipping reachability check"
    # normalise IfcWallStandardCase -> also accept IfcWall
    candidates = {entity_type}
    if entity_type == "IfcWallStandardCase":
        candidates.add("IfcWall")
    for cand in candidates:
        if re.search(rf'by_type\(\s*[\'"]{re.escape(cand)}[\'"]', checker_source):
            return True, f"checker queries {cand} (matches fixture's perturbed entity type)"
    return False, (
        f"checker source never calls model.by_type(\"{entity_type}\") (or an alias) -- "
        f"it cannot see the entity type this fixture perturbed"
    )


def _extract_target_guid(meta: dict | None) -> str | None:
    if not meta:
        return None
    for key in GUID_KEY_CANDIDATES:
        if meta.get(key):
            return meta[key]
    return None


def _violations_text(result: dict) -> str:
    return json.dumps(result.get("violations", []))


def run_case(mod, case: FixtureCase, checker_source: str) -> FixtureResult:
    import ifcopenshell

    entity_type = (case.meta or {}).get("element_type") if case.meta else None
    reachable, reach_note = check_reachability(checker_source, entity_type)

    model = ifcopenshell.open(str(case.ifc_path))
    try:
        result = mod.check_rule(model)
    except Exception as exc:  # noqa: BLE001
        return FixtureResult(
            case, verdict="ERROR", reachable=reachable, reachability_note=reach_note,
            target_guid=None, target_found=None, ok=False,
            diagnostic=f"checker raised an exception: {exc!r}",
        )

    verdict = result.get("verdict", "unknown")

    if case.category == "negative":
        # Negative fixtures are informational only (real baselines can have
        # genuine pre-existing violations) -- the only hard requirement is
        # that the checker runs without error and the entity type it needs
        # is reachable (a crash-free run).
        return FixtureResult(
            case, verdict=verdict, reachable=reachable, reachability_note=reach_note,
            target_guid=None, target_found=None, ok=True,
        )

    target_guid = _extract_target_guid(case.meta)
    target_found = None
    if target_guid:
        target_found = target_guid in _violations_text(result)

    if not reachable:
        return FixtureResult(
            case, verdict=verdict, reachable=False, reachability_note=reach_note,
            target_guid=target_guid, target_found=target_found, ok=False,
            diagnostic=(
                f"UNREACHABLE: {reach_note}. Fixture meta: "
                f"{json.dumps(case.meta, indent=None)[:300]}"
            ),
        )

    if verdict != "fail":
        return FixtureResult(
            case, verdict=verdict, reachable=reachable, reachability_note=reach_note,
            target_guid=target_guid, target_found=target_found, ok=False,
            diagnostic=(
                f"WRONG VERDICT: expected 'fail' on a Positive fixture, got '{verdict}'. "
                f"Fixture meta: {json.dumps(case.meta, indent=None)[:300]}"
            ),
        )

    if target_guid and target_found is False:
        return FixtureResult(
            case, verdict=verdict, reachable=reachable, reachability_note=reach_note,
            target_guid=target_guid, target_found=False, ok=False,
            diagnostic=(
                f"FALSE-CONFIRMATION: verdict is 'fail' but the specific injected "
                f"element (GUID {target_guid}) never appears in the violations list -- "
                f"this FAIL is coming from something else in the model, not the "
                f"injected violation. Fixture meta: {json.dumps(case.meta, indent=None)[:300]}"
            ),
        )

    return FixtureResult(
        case, verdict=verdict, reachable=reachable, reachability_note=reach_note,
        target_guid=target_guid, target_found=target_found, ok=True,
    )


def run_rule(rule_id: str, checker_path: Path) -> RuleReport:
    checker_source = checker_path.read_text(encoding="utf-8")
    mod = _load_checker_module(checker_path)
    cases = load_fixtures(rule_id)
    report = RuleReport(rule_id=rule_id, checker_path=checker_path)
    for case in cases:
        report.results.append(run_case(mod, case, checker_source))
    return report


def print_report(report: RuleReport) -> None:
    print(f"=== Rule {report.rule_id} — {report.checker_path} ===")
    for r in report.results:
        mark = "OK  " if r.ok else "FAIL"
        print(f"[{mark}] {r.case.category:8} {r.case.building:26} verdict={r.verdict:14} "
              f"reachable={r.reachable} target_found={r.target_found}")
    print()
    if report.accepted:
        print("VERDICT: ACCEPTED (every positive fixture attributes to its exact injected element)")
    else:
        print("VERDICT: REJECTED")
        print("FEEDBACK:")
        print(report.feedback_text())
