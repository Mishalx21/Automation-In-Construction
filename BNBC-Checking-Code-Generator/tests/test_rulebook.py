"""Rulebook tests: the checked-in build, reference resolution, rendering, and
the guarantees that make the layer worth having — no invented content, no
labels reachable from the agent.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bnbc import config as cfg
from bnbc.rulebook import (
    Rulebook,
    RulebookError,
    find_references,
    render_context,
    render_reference,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rulebook() -> Rulebook:
    if not (cfg.RULEBOOK_DIR / "clauses").exists():
        pytest.skip("no rulebook (see docs/RULEBOOK-FORMAT.md)")
    return Rulebook()


class TestReferenceParsing:
    @pytest.mark.parametrize("text,expected", [
        ("see Table 6.8.1 for values", {"tables": ["6.8.1"]}),
        ("as shown in Figure 6.8.2.", {"figures": ["6.8.2"]}),
        ("computed by Eq. 6.8.6", {"equations": ["6.8.6"]}),
        ("as specified in Sec 8.3.5.4", {"clauses": ["8.3.5.4"]}),
        ("no citations here", {}),
    ])
    def test_finds_each_citation_style(self, text, expected):
        assert find_references(text) == expected

    def test_deduplicates_and_sorts(self):
        found = find_references("Table 6.8.1 and table 6.8.1", "Table 6.8.3")
        assert found["tables"] == ["6.8.1", "6.8.3"]


class TestCheckedInBuild:
    """The build is committed, so these assert what the agent will actually read."""

    def test_every_clause_is_citable_to_a_page(self, rulebook):
        """A generated checker must be traceable back to the code it came
        from, so page numbers are load-bearing, not decoration."""
        for section in rulebook._load_sections().values():
            for clause in section["clauses"]:
                assert clause.get("page"), f"{clause['clause_id']} has no page"
                assert clause.get("text", "").strip()

    def test_clauses_are_in_reading_order(self, rulebook):
        ids = [c["clause_id"] for c in rulebook.section("8.1.6")["clauses"]]
        assert ids == sorted(ids, key=lambda i: [int(p) for p in i.split(".")])

    def test_table_reaches_the_prompt_as_markdown(self, rulebook):
        """HTML makes a model parse tags instead of reading bands."""
        table = rulebook.table("6.8.1")
        assert "| ---" in table["markdown"]
        assert "10 mm ≤ d_b ≤ 25 mm" in table["markdown"]
        assert "<td" not in table["markdown"]

    def test_equation_is_readable_not_latex(self, rulebook):
        """A model reading LaTeX escapes spends attention on syntax."""
        equation = rulebook.equation("6.8.6")
        assert equation["readable"] == "ρ_s = (0.12 f'_c)/(f_yt)"
        assert "\\frac" not in equation["readable"]

    def test_figure_is_a_file_never_inline_base64(self, rulebook):
        figure = rulebook.figure("6.8.2")
        assert Path(figure["image_path"]).exists()
        assert "base64" not in json.dumps(figure)

    def test_terms_are_addressable_and_defined(self, rulebook):
        terms = rulebook.terms()
        assert terms, "the glossary should not be empty"
        for term_id, term in terms.items():
            assert term["term_id"] == term_id
            assert term["definition"].strip()


class TestResolution:
    def test_resolves_a_rule_bundle(self, rulebook):
        rule = json.loads((cfg.RULES_DIR / "8.1.2.2" / "rule.json").read_text(encoding="utf-8"))
        context = rulebook.resolve(
            references=rule["references"],
            clause_ids=rule["source_clauses"],
            term_ids=rule["terms"],
        )
        assert [c["clause_id"] for c in context.clauses] == ["8.1.2.2(a)", "8.1.2.2(b)"]
        assert [t["table_id"] for t in context.tables] == ["6.8.1"]
        assert not context.missing

    def test_a_section_citation_pulls_in_its_clauses(self, rulebook):
        context = rulebook.resolve(references={"clauses": ["8.1.9.4"]})
        assert len(context.clauses) > 1  # a whole section, not one clause

    def test_citations_resolve_transitively(self, rulebook):
        """8.3.7.2 requires "one-half the amount required by Sec 8.3.5.4(a)",
        and that section defines the amount by Eq. 6.8.6. One hop would hand
        the drafter a cross-reference to a formula it cannot see."""
        rule = json.loads((cfg.RULES_DIR / "8.3.7.2" / "rule.json").read_text(encoding="utf-8"))
        kwargs = dict(references=rule["references"], clause_ids=rule["source_clauses"])

        one_hop = rulebook.resolve(**kwargs, depth=1)
        assert [e["equation_id"] for e in one_hop.equations] == []

        closure = rulebook.resolve(**kwargs)
        assert [e["equation_id"] for e in closure.equations] == ["6.8.6", "6.8.7", "6.8.8"]
        assert not closure.missing

    def test_resolution_terminates_on_a_citation_cycle(self, rulebook):
        """Clause A citing B citing A must not loop; the seen-set is what
        makes depth a bound rather than a hope."""
        rulebook = Rulebook(rulebook.root)
        rulebook._sections = {
            "S": {"section_id": "S", "clauses": [
                {"clause_id": "A", "text": "see Sec B", "references": {"clauses": ["B"]}},
                {"clause_id": "B", "text": "see Sec A", "references": {"clauses": ["A"]}},
            ]}
        }
        rulebook._clause_index = {
            c["clause_id"]: ("S", c) for c in rulebook._sections["S"]["clauses"]
        }
        context = rulebook.resolve(clause_ids=["A"], depth=10)
        assert [c["clause_id"] for c in context.clauses] == ["A", "B"]

    def test_unknown_reference_is_reported_not_raised(self, rulebook):
        context = rulebook.resolve(references={"tables": ["9.9.9"]})
        assert context.tables == []
        assert context.missing == ["table 9.9.9"]

    def test_missing_rulebook_is_a_setup_error(self, tmp_path):
        with pytest.raises(RulebookError, match="RULEBOOK-FORMAT"):
            Rulebook(tmp_path).clause("8.1.6.1")


class TestRendering:
    def test_context_renders_every_section_it_has(self, rulebook):
        rule = json.loads((cfg.RULES_DIR / "8.3.5.4.A.D" / "rule.json").read_text(encoding="utf-8"))
        text = render_context(rulebook.resolve(
            references=rule["references"],
            clause_ids=rule["source_clauses"],
            term_ids=rule["terms"],
        ))
        assert "### Source clauses" in text
        assert "### Referenced equations" in text
        assert "ρ_s = (0.12 f'_c)/(f_yt)" in text
        assert "BNBC p." in text  # every clause is citable back to a page

    def test_unresolved_references_warn_against_invention(self, rulebook):
        text = render_context(rulebook.resolve(references={"tables": ["9.9.9"]}))
        assert "do not invent" in text.lower()

    def test_reference_rendering_is_kind_agnostic(self, rulebook):
        assert "Table 6.8.1" in render_reference("table", rulebook.table("6.8.1"))
        assert "8.1.6.1" in render_reference("clause", rulebook.clause("8.1.6.1"))


class TestRuleDefinitions:
    """The 19 rule definitions are inputs; these are their invariants."""

    def test_every_rule_is_well_formed_and_label_free(self, rulebook):
        expected = {"rule_id", "title", "source_clauses", "statement",
                    "scope_note", "references", "terms"}
        rules = sorted(cfg.RULES_DIR.glob("*/rule.json"))
        assert len(rules) == 19
        for path in rules:
            rule = json.loads(path.read_text(encoding="utf-8"))
            assert set(rule) == expected, f"{path.parent.name}: {set(rule) ^ expected}"
            assert rule["statement"].strip(), path.parent.name
            assert rule["source_clauses"], path.parent.name
            assert "test_cases" not in rule

    def test_every_source_clause_exists_in_the_rulebook(self, rulebook):
        for path in sorted(cfg.RULES_DIR.glob("*/rule.json")):
            rule = json.loads(path.read_text(encoding="utf-8"))
            for clause_id in rule["source_clauses"]:
                assert rulebook.clause(clause_id) is not None, \
                    f"{rule['rule_id']} cites unknown clause {clause_id}"

    def test_every_reference_resolves(self, rulebook):
        for path in sorted(cfg.RULES_DIR.glob("*/rule.json")):
            rule = json.loads(path.read_text(encoding="utf-8"))
            context = rulebook.resolve(references=rule["references"])
            assert not context.missing, f"{rule['rule_id']}: {context.missing}"

    def test_no_labels_anywhere_in_the_repository(self):
        """The 'no labelled test cases' claim is structural: the labels live in
        the Code-Agent repository, and one dev-only script reads them from
        there. Nothing here holds a copy, and nothing in the package looks."""
        for rule_file in REPO_ROOT.glob("rules/*/rule.json"):
            assert "test_cases" not in json.loads(rule_file.read_text(encoding="utf-8"))
        assert not (REPO_ROOT / "eval").exists(), "labels must not be copied into this repo"

        forbidden = {"test_cases", "expected_result"}
        for source in (REPO_ROOT / "bnbc").rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            live = {
                node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings
            }
            leaked = {s for s in live if any(f in s for f in forbidden)}
            assert not leaked, f"{source.relative_to(REPO_ROOT)} reaches for labels: {leaked}"

    def test_only_the_dev_eval_script_touches_labels(self):
        """One file, clearly marked, and it reads them from Code-Agent."""
        readers = [
            p for p in REPO_ROOT.glob("scripts/*.py")
            if "test_cases" in p.read_text(encoding="utf-8")
        ]
        assert [p.name for p in readers] == ["evaluate_v2.py"]
        assert "Code-Agent" in readers[0].read_text(encoding="utf-8")
