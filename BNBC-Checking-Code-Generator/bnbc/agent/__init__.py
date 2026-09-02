"""The rule-checker agent.

A fixed LangGraph workflow — ingest -> spec_card -> fixture_plan -> retrieve
-> agentic_draft -> execute -> (conformance -> store | spec_revise | reject)
— around one tool-using drafter session and one deterministic oracle. Every
loop is bounded and every path terminal.

Entry point: :func:`bnbc.agent.graph.run_rule`. See docs/ARCHITECTURE.md.
"""
