"""LangSmith tracing for the LLM layer.

LangGraph nodes are traced automatically once ``LANGSMITH_TRACING=true``
(``config`` exports the ``LANGCHAIN_*`` variables the tracer reads). Model
calls no longer pass through LangChain, so they are traced explicitly with
``@traced_llm`` on :func:`bnbc.llm.call.call_llm` — without it a run would show
the graph but not a single prompt, token count, or latency.

Both the decorator and the SDK degrade to no-ops: tracing off, or ``langsmith``
not installed, must never change behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger("bnbc.llm.tracing")

F = TypeVar("F", bound=Callable[..., Any])


def _noop(fn: F) -> F:
    return fn


def traced_llm(name: str) -> Callable[[F], F]:
    """Decorate an LLM call so LangSmith records it as an ``llm`` run."""
    try:
        from langsmith import traceable
    except Exception as exc:  # pragma: no cover — langsmith is optional
        logger.debug("langsmith unavailable, tracing disabled: %s", exc)
        return _noop
    return traceable(run_type="llm", name=name)
