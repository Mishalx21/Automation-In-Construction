"""Gemini backend: multi-key pool, key rotation, model rotation.

Resilience contract (all of it local to this module — nodes never see it):

Two independent quotas are in play, per key and per model, so both rotate —
together they form one throughput budget much larger than either alone.

* **key pool** — ``GEMINI_API_KEYS`` may hold many keys (JSON array,
  bracketed list, or comma-separated). A key the API reports as invalid is
  marked dead for the process; a key reporting a quota/rate limit is rotated
  past immediately, without sleeping, because another key usually has quota.
* **model rotation with cooldown** — Gemini quota is *per model*, so a
  rate-limited or overloaded model is **parked** for
  ``GEMINI_MODEL_COOLDOWN_SECONDS`` and the next model in
  ``GEMINI_MODEL_FALLBACKS`` is used. Hammering one limited model through the
  whole key pool wastes the rule's wall-clock budget while several other
  models sit idle with quota to spare. If every model is cooling the loop
  waits for the first to free up — waiting beats failing, and ``call_llm``'s
  wall-clock ceiling is the outer bound.
* **config degradation** — a 400 that names the request *configuration*
  (native JSON-schema dialect, thinking config) is retried once with a
  minimal config, restating the output contract in the prompt instead. This
  keeps schemas the native structured-output dialect cannot express (free-form
  ``dict`` operator params in the spec card) working on every model.

Every exhausted path raises an ``LLMError`` subclass so the graph guard can
end the rule cleanly instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from threading import Lock
from typing import Any, Callable, Sequence

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
    schema_instruction,
    validate_structured,
)

logger = logging.getLogger("bnbc.llm.gemini")

#: reasoning_effort -> Gemini thinking level. "none" maps to MINIMAL: the API
#: has no "off" for thinking models, and MINIMAL is the documented floor.
_THINKING_LEVELS = {
    "none": "MINIMAL",
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}


def parse_api_keys(raw: str | None) -> list[str]:
    """Parse a key list: ``[a, b]``, ``"a", "b"``, ``a,b``, or a single key.

    Every element is unquoted and stripped: pasting a pool out of a JSON
    document or a spreadsheet is the normal way these lists arrive, and a
    stray quote would silently turn every key into an invalid one.
    """
    value = (raw or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    keys = []
    for part in value.split(","):
        key = part.strip().strip('"').strip("'").strip()
        if key:
            keys.append(key)
    return keys


def _mask(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"


class KeyPool:
    """Round-robin pool of API keys with dead-key exclusion."""

    def __init__(self, keys: Sequence[str]):
        self._keys = list(keys)
        if not self._keys:
            raise LLMProviderError(
                "no Gemini API keys configured — set GEMINI_API_KEYS "
                "(comma-separated for a pool) in .env"
            )
        self._lock = Lock()
        self._dead: set[int] = set()
        # Start at a random offset so parallel processes do not all hammer key 0.
        self._index = random.randrange(len(self._keys))

    def __len__(self) -> int:
        return len(self._keys)

    def current(self) -> str:
        with self._lock:
            for _ in range(len(self._keys)):
                if self._index not in self._dead:
                    return self._keys[self._index]
                self._index = (self._index + 1) % len(self._keys)
            raise LLMProviderError(
                f"all {len(self._keys)} Gemini API key(s) in the pool are invalid"
            )

    def rotate(self) -> None:
        with self._lock:
            self._index = (self._index + 1) % len(self._keys)

    def mark_dead(self, key: str) -> None:
        with self._lock:
            try:
                idx = self._keys.index(key)
            except ValueError:
                return
            self._dead.add(idx)
            logger.warning(
                "Gemini key [%d] %s marked dead; %d/%d still alive",
                idx, _mask(key), len(self._keys) - len(self._dead), len(self._keys),
            )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _classify(exc: Exception) -> str:
    text = str(exc).lower()
    if "api_key_invalid" in text or "api key not valid" in text or "unauthenticated" in text:
        return "invalid_key"
    if "503" in text or "service unavailable" in text or "overloaded" in text:
        return "overloaded"
    if "429" in text or "resource_exhausted" in text or "quota" in text or "rate limit" in text:
        return "rate_limit"
    if "400" in text or "invalid_argument" in text:
        # Only *configuration* 400s are worth degrading; anything else 400 is
        # a genuine request bug and must surface.
        if any(k in text for k in ("schema", "thinking", "response_json", "response_mime",
                                   "function_declaration", "tool")):
            return "bad_config"
    return "other"


# ---------------------------------------------------------------------------
# Message / tool translation
# ---------------------------------------------------------------------------

def _to_gemini(messages: Sequence[Message]) -> tuple[str, list[Any]]:
    """(system_instruction, contents) for the Gemini SDK."""
    from google.genai import types

    system_parts: list[str] = []
    contents: list[Any] = []
    for msg in messages:
        if msg.role == "system":
            if msg.content:
                system_parts.append(msg.content)
            continue
        if msg.role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=msg.content)])
            )
            continue
        if msg.role == "assistant":
            parts: list[Any] = []
            if msg.content:
                parts.append(types.Part(text=msg.content))
            for call in msg.tool_calls:
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(name=call.name, args=dict(call.args))
                    )
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
            continue
        # role == "tool": a function response is delivered as a user turn.
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_function_response(
                        name=msg.tool_name or "tool", response={"result": msg.content}
                    )
                ],
            )
        )
    return "\n\n".join(system_parts), contents


def _to_gemini_tools(tools: Sequence[ToolSpec]) -> list[Any]:
    from google.genai import types

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters_json_schema=t.parameters,
                )
                for t in tools
            ]
        )
    ]


def _read_response(response: Any) -> tuple[str, tuple[ToolCall, ...], Usage]:
    """Pull text, tool calls, and usage out of a Gemini response."""
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        content = getattr(candidates[0], "content", None)
        for i, part in enumerate(getattr(content, "parts", None) or []):
            if getattr(part, "thought", False):
                continue
            if getattr(part, "text", None):
                text_parts.append(part.text)
            call = getattr(part, "function_call", None)
            if call is not None and getattr(call, "name", None):
                calls.append(
                    ToolCall(
                        id=getattr(call, "id", None) or f"call_{i}",
                        name=call.name,
                        args=dict(getattr(call, "args", None) or {}),
                    )
                )
    meta = getattr(response, "usage_metadata", None)
    usage = Usage(
        input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0)
        + int(getattr(meta, "thoughts_token_count", 0) or 0),
    )
    return "".join(text_parts), tuple(calls), usage


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    """Google Gemini via the ``google-genai`` SDK."""

    name = "gemini"

    def __init__(
        self,
        api_keys: Sequence[str] | None = None,
        fallback_models: Sequence[str] | None = None,
        cooldown_seconds: float | None = None,
    ):
        keys = list(api_keys) if api_keys is not None else parse_api_keys(
            os.environ.get("GEMINI_API_KEYS")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self._pool = KeyPool(keys)
        self._fallback_models = list(
            fallback_models if fallback_models is not None else cfg.GEMINI_MODEL_FALLBACKS
        )
        self._clients: dict[str, Any] = {}
        #: model -> monotonic time it may be used again. Gemini quota is per
        #: model, so a limited model is parked rather than retried to death.
        self._cooling: dict[str, float] = {}
        self._cooldown_s = (
            cooldown_seconds if cooldown_seconds is not None else cfg.GEMINI_MODEL_COOLDOWN_SECONDS
        )
        logger.info(
            "GeminiProvider ready: %d key(s) %s, model fallbacks %s",
            len(self._pool), [_mask(k) for k in keys], self._fallback_models,
        )

    def preflight(self) -> None:
        """The key pool was validated in __init__; check the SDK is present."""
        try:
            import google.genai  # noqa: F401
        except ImportError as exc:
            raise LLMProviderError(
                "google-genai is not installed — run "
                "`pip install -r requirements.txt`"
            ) from exc

    # -- client cache --------------------------------------------------
    def _client(self, api_key: str) -> Any:
        client = self._clients.get(api_key)
        if client is None:
            self.preflight()
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=cfg.LLM_REQUEST_TIMEOUT_SECONDS * 1000  # ms
                ),
            )
            self._clients[api_key] = client
        return client

    # -- config --------------------------------------------------------
    def _config(
        self,
        *,
        system_instruction: str,
        temperature: float,
        max_output_tokens: int,
        structured: type[BaseModel] | None,
        tools: Sequence[ToolSpec] | None,
        reasoning_effort: str,
        degraded: bool,
    ) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {"temperature": temperature}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens

        level = _THINKING_LEVELS.get((reasoning_effort or "").lower())
        if level and not degraded:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)

        if structured is not None:
            kwargs["response_mime_type"] = "application/json"
            if not degraded:
                kwargs["response_json_schema"] = json_schema_of(structured)
        elif tools:
            kwargs["tools"] = _to_gemini_tools(tools)
        return types.GenerateContentConfig(**kwargs)

    # -- the call ------------------------------------------------------
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

        system_instruction, contents = _to_gemini(messages)
        degraded_instruction = (
            f"{system_instruction}\n\n{schema_instruction(structured)}".strip()
            if structured is not None
            else system_instruction
        )

        async def attempt(client: Any, model_name: str, degraded: bool) -> Any:
            return await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=self._config(
                    system_instruction=degraded_instruction if degraded else system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    structured=structured,
                    tools=tools,
                    reasoning_effort=reasoning_effort,
                    degraded=degraded,
                ),
            )

        response, used_model = await self._execute(attempt, model)
        text, calls, usage = _read_response(response)

        parsed = None
        if structured is not None:
            try:
                parsed = validate_structured(structured, text)
            except LLMOutputError as exc:
                # The tokens were spent whether or not the reply parsed — the
                # caller meters them off the exception before deciding.
                raise LLMOutputError(str(exc), call_usage=usage.as_dict())
        return LLMResponse(
            text=text, tool_calls=calls, parsed=parsed, usage=usage, model=used_model
        )

    # -- rotation loop -------------------------------------------------
    def _model_sequence(self, preferred_model: str) -> list[str]:
        return [preferred_model] + [
            m for m in self._fallback_models if m != preferred_model
        ]

    def _next_model(self, models: list[str]) -> tuple[str | None, float]:
        """The first model not cooling down, and how long until one frees up."""
        now = time.monotonic()
        soonest = None
        for model in models:
            until = self._cooling.get(model, 0.0)
            if until <= now:
                return model, 0.0
            soonest = until if soonest is None else min(soonest, until)
        return None, max(0.0, (soonest or now) - now)

    def _park(self, model: str, reason: str) -> None:
        """Take a model out of rotation briefly.

        Quota on Gemini is per model, so a rate-limited model is not a reason
        to stop working — it is a reason to work on a different one. Parking
        it (rather than hammering it through the whole key pool) is what turns
        several models' separate quotas into one usable throughput budget.
        """
        self._cooling[model] = time.monotonic() + self._cooldown_s
        logger.warning(
            "Gemini model %s parked for %.0fs (%s) — continuing on another model",
            model, self._cooldown_s, reason,
        )

    async def _execute(
        self, attempt: Callable[[Any, str, bool], Any], preferred_model: str
    ) -> tuple[Any, str]:
        """Run ``attempt`` across the model chain and the key pool.

        Two independent quotas are in play — per key and per model — so both
        rotate. A key that reports a quota error is rotated past; a model that
        does is parked (:meth:`_park`) and another is used. The whole loop sits
        inside ``call_llm``'s wall-clock ceiling, which is the real bound.
        """
        models = self._model_sequence(preferred_model)
        budget = max(6, len(models) * 3)
        degraded: set[str] = set()
        invalid_keys_seen = 0
        last_error: Exception | None = None

        for _ in range(budget):
            model_name, wait = self._next_model(models)
            if model_name is None:
                # Every model is cooling: waiting beats failing, and the
                # caller's wall timeout stops this from waiting forever.
                logger.info("Gemini: all %d model(s) cooling, waiting %.0fs", len(models), wait)
                await asyncio.sleep(min(wait, self._cooldown_s))
                continue

            # Pool exhaustion and a missing SDK are configuration faults:
            # resolved outside the try so they surface immediately instead of
            # being rotated over as if they were transient.
            key = self._pool.current()
            client = self._client(key)
            try:
                return await attempt(client, model_name, model_name in degraded), model_name
            except Exception as exc:  # vendor exceptions are opaque by design
                last_error = exc
                kind = _classify(exc)

                if kind == "invalid_key":
                    self._pool.mark_dead(key)
                    self._pool.rotate()
                    invalid_keys_seen += 1
                    if invalid_keys_seen > len(self._pool):
                        break
                    continue

                if kind == "bad_config" and model_name not in degraded:
                    logger.warning(
                        "Gemini rejected the request config on %s (%s) — retrying "
                        "with a minimal config and the contract restated in the prompt",
                        model_name, exc,
                    )
                    degraded.add(model_name)
                    continue

                if kind in ("rate_limit", "overloaded"):
                    if kind == "rate_limit":
                        self._pool.rotate()  # the key's quota may be spent too
                    self._park(model_name, kind)
                    continue

                logger.warning(
                    "Gemini call failed on %s with key %s: %s — rotating key",
                    model_name, _mask(key), exc,
                )
                self._pool.rotate()

        raise LLMProviderError(
            f"Gemini exhausted every model {models} and {len(self._pool)} key(s); "
            f"last error: {last_error}"
        )
