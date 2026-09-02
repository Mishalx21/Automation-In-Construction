"""OpenRouter backend (OpenAI-compatible chat completions).

Kept alongside the Gemini backend so the pipeline can be re-pointed at the
OpenRouter model zoo with ``LLM_PROVIDER=openrouter`` — no node changes.
OpenRouter does its own upstream routing and failover, so this provider adds
only what the API itself does not: request shaping (reasoning cap, provider
pin, completion ceiling) and the structured-output dialect each family
actually honours.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from pydantic import BaseModel

from bnbc import config as cfg
from bnbc.llm.base import (
    LLMOutputError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    json_schema_of,
    validate_structured,
)

logger = logging.getLogger("bnbc.llm.openrouter")

#: Name of the synthetic tool used to force structured output on families
#: whose ``response_format`` support is unreliable through OpenRouter.
_EMIT_TOOL = "emit_result"


def _to_openai(messages: Sequence[Message]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "tool":
            payload.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id or msg.tool_name,
                "content": msg.content,
            })
            continue
        if msg.role == "assistant" and msg.tool_calls:
            payload.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.args, ensure_ascii=False),
                        },
                    }
                    for call in msg.tool_calls
                ],
            })
            continue
        payload.append({"role": msg.role, "content": msg.content})
    return payload


def _to_openai_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _uses_tool_calling_for_schema(model: str) -> bool:
    """Gemini endpoints on OpenRouter do not populate ``response_format``
    reliably; tool-calling structured output does (verified live)."""
    return model.startswith("google/")


class OpenRouterProvider(LLMProvider):
    """OpenRouter via the OpenAI-compatible API."""

    name = "openrouter"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key if api_key is not None else cfg.OPENROUTER_API_KEY
        self._base_url = base_url or cfg.OPENROUTER_BASE_URL
        if not self._api_key:
            raise LLMProviderError(
                "no OpenRouter API key configured — set OPENROUTER_API_KEY in .env"
            )
        self._client: Any = None

    def preflight(self) -> None:
        """The key was validated in __init__; check the SDK is present."""
        try:
            import openai  # noqa: F401
        except ImportError as exc:
            raise LLMProviderError(
                "openai is not installed — run `pip install -r requirements.txt`"
            ) from exc

    def _get_client(self) -> Any:
        if self._client is None:
            self.preflight()
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=cfg.LLM_REQUEST_TIMEOUT_SECONDS,
                max_retries=2,
            )
        return self._client

    async def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        temperature: float = 0.0,
        max_output_tokens: int = 0,
        structured: type[BaseModel] | None = None,
        tools: Sequence[ToolSpec] | None = None,
        reasoning_effort: str = "",
    ) -> LLMResponse:
        if structured is not None and tools:
            raise LLMProviderError("structured output and tools are mutually exclusive")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _to_openai(messages),
            "temperature": temperature,
        }
        if max_output_tokens:
            kwargs["max_tokens"] = max_output_tokens

        extra_body: dict[str, Any] = {}
        if cfg.OPENROUTER_PROVIDER:
            extra_body["provider"] = {"order": [cfg.OPENROUTER_PROVIDER]}
        if reasoning_effort == "none":
            extra_body["reasoning"] = {"enabled": False}
        elif reasoning_effort:
            extra_body["reasoning"] = {"effort": reasoning_effort}
        if extra_body:
            kwargs["extra_body"] = extra_body

        schema_via_tool = False
        if structured is not None:
            schema = json_schema_of(structured)
            if _uses_tool_calling_for_schema(model):
                schema_via_tool = True
                kwargs["tools"] = [{
                    "type": "function",
                    "function": {
                        "name": _EMIT_TOOL,
                        "description": f"Emit the {structured.__name__} result.",
                        "parameters": schema,
                    },
                }]
                kwargs["tool_choice"] = {
                    "type": "function", "function": {"name": _EMIT_TOOL}
                }
            else:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": structured.__name__, "schema": schema},
                }
        elif tools:
            kwargs["tools"] = _to_openai_tools(tools)

        try:
            response = await self._get_client().chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMProviderError(f"OpenRouter call to {model} failed: {exc}")

        usage_obj = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMOutputError(
                f"OpenRouter returned no choices for {model}", call_usage=usage.as_dict()
            )
        message = choices[0].message
        text = message.content or ""
        calls = tuple(
            ToolCall(
                id=tc.id or f"call_{i}",
                name=tc.function.name,
                args=_loads(tc.function.arguments),
            )
            for i, tc in enumerate(getattr(message, "tool_calls", None) or [])
        )

        parsed = None
        if structured is not None:
            payload = calls[0].args if (schema_via_tool and calls) else None
            try:
                if payload is not None:
                    parsed = structured.model_validate(payload)
                else:
                    parsed = validate_structured(structured, text)
            except LLMOutputError as exc:
                raise LLMOutputError(str(exc), call_usage=usage.as_dict())
            except Exception as exc:
                raise LLMOutputError(
                    f"reply did not validate against {structured.__name__}: {exc}",
                    call_usage=usage.as_dict(),
                )
            calls = ()  # the emit tool is plumbing, never a caller-visible call

        return LLMResponse(
            text=text, tool_calls=calls, parsed=parsed, usage=usage, model=model
        )


def _loads(arguments: str | None) -> dict[str, Any]:
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
