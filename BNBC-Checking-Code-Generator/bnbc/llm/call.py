"""The single metered entry point every pipeline node uses.

Robustness contract (earned from live incidents, see ARCHITECTURE.md):

* every attempt is bounded by a WALL-CLOCK ceiling — a socket read timeout
  cannot stop a slow-but-alive generation;
* usage is metered on FAILED calls too (a parse failure that burned 65K
  tokens must count against the budget);
* ANY exception leaving this function carries the meter snapshot, so the
  graph guard can persist accounting on terminal paths;
* a structured call that fails once is retried WITHOUT the native schema,
  restating the contract in the prompt and validating locally — providers
  disagree about which schema dialects they accept, the output contract does
  not;
* a wall timeout is never retried (the retry would take just as long).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Sequence

from pydantic import BaseModel

from bnbc import config as cfg
from bnbc.llm.base import (
    BudgetExceeded,
    LLMError,
    LLMResponse,
    LLMWallTimeout,
    Message,
    ToolSpec,
    schema_instruction,
    user,
    validate_structured,
)
from bnbc.llm.metering import TokenMeter
from bnbc.llm.registry import get_provider
from bnbc.llm.tracing import traced_llm

logger = logging.getLogger("bnbc.llm.call")


@traced_llm("call_llm")
async def call_llm(
    model_name: str,
    messages: Sequence[Message],
    meter: TokenMeter,
    node: str,
    *,
    temperature: float = 0.0,
    structured: type[BaseModel] | None = None,
    reasoning_effort: str = "",
    tools: Sequence[ToolSpec] | None = None,
) -> Any:
    """One metered call. Returns the parsed model (structured) or the reply."""
    provider = get_provider()
    wall = cfg.LLM_WALL_TIMEOUT_SECONDS
    started = time.perf_counter()

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _attach(exc: LLMError) -> LLMError:
        if not exc.usage:
            try:
                exc.usage = meter.to_state()
            except Exception:  # pragma: no cover — accounting must never mask the error
                pass
        return exc

    async def _invoke(
        schema: type[BaseModel] | None, payload: Sequence[Message]
    ) -> LLMResponse:
        try:
            return await asyncio.wait_for(
                provider.generate(
                    model=model_name,
                    messages=payload,
                    temperature=temperature,
                    max_output_tokens=cfg.LLM_MAX_COMPLETION_TOKENS,
                    structured=schema,
                    tools=tools,
                    reasoning_effort=reasoning_effort,
                ),
                timeout=wall,
            )
        except asyncio.TimeoutError:
            raise LLMWallTimeout(
                f"LLM call exceeded the wall-clock ceiling of {wall}s "
                f"(model {model_name}, node {node}; env LLM_WALL_TIMEOUT_SECONDS)"
            )

    try:
        response = await _invoke(structured, list(messages))
    except BudgetExceeded:
        raise
    except LLMWallTimeout as exc:
        raise _attach(exc)
    except LLMError as exc:
        if exc.call_usage:
            meter.add(node, exc.call_usage, model_name=model_name, latency_ms=_elapsed_ms())
        if structured is None:
            raise _attach(exc)

        logger.warning(
            "Structured call to %s failed (%s); retrying without the native schema",
            model_name, exc,
        )
        retry_messages = list(messages) + [user(schema_instruction(structured))]
        try:
            response = await _invoke(None, retry_messages)
        except BudgetExceeded:
            raise
        except LLMError as retry_exc:
            if retry_exc.call_usage:
                meter.add(node, retry_exc.call_usage, model_name=model_name,
                          latency_ms=_elapsed_ms())
            raise _attach(retry_exc)

        meter.add(node, response.usage.as_dict(), model_name=response.model or model_name,
                  latency_ms=_elapsed_ms())
        try:
            return validate_structured(structured, response.text)
        except LLMError as parse_exc:
            logger.error(
                "Schema-less retry also failed to parse. Reply head: %s",
                response.text[:2000],
            )
            raise _attach(parse_exc)

    meter.add(node, response.usage.as_dict(), model_name=response.model or model_name,
              latency_ms=_elapsed_ms())
    return response.parsed if structured is not None else response
