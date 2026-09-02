"""BNBC checking-code generator.

Generates Python rule checkers for Bangladesh National Building Code
provisions and verifies them against manufactured ground truth (FIV —
Fixture-Injected Verification).

Layout:

* :mod:`bnbc.contracts` — the data contracts every layer shares
* :mod:`bnbc.llm`       — provider-agnostic LLM layer (Gemini, OpenRouter)
* :mod:`bnbc.fixtures`  — the deterministic fixture engine and acceptance gate
* :mod:`bnbc.agent`     — the LangGraph agent that turns a rule into a checker

``ifc_helpers`` (repository root) is deliberately outside this package: it is
the runtime library the *generated* checkers import, not part of the agent.
"""
