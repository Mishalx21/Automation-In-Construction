"""Provider-layer tests: error classification, key/model rotation, config
degradation, and message translation. No network and no SDK required for the
rotation tests — the vendor call is stubbed at the seam.
"""

from __future__ import annotations

import asyncio

import pytest

from bnbc.llm.base import (
    LLMProviderError,
    ToolCall,
    assistant,
    extract_json,
    system,
    tool_result,
    user,
)
from bnbc.llm.gemini import GeminiProvider, _classify


def _provider(keys=("k1", "k2"), fallbacks=("m2", "m3"), cooldown=60.0) -> GeminiProvider:
    provider = GeminiProvider(api_keys=list(keys), fallback_models=list(fallbacks),
                              cooldown_seconds=cooldown)
    # The rotation loop only needs an object to hand to `attempt`.
    provider._client = lambda key: key  # type: ignore[assignment]
    return provider


class TestErrorClassification:
    @pytest.mark.parametrize("text,kind", [
        ("400 API_KEY_INVALID: API key not valid", "invalid_key"),
        ("503 Service Unavailable: model is overloaded", "overloaded"),
        ("429 RESOURCE_EXHAUSTED: quota exceeded", "rate_limit"),
        ("400 INVALID_ARGUMENT: response_json_schema not supported", "bad_config"),
        ("500 internal error", "other"),
    ])
    def test_classifies_vendor_errors(self, text, kind):
        assert _classify(RuntimeError(text)) == kind


class TestRotation:
    def test_rate_limit_parks_the_model_and_moves_to_the_next(self):
        """Quota is per model: a limited model must not be hammered through
        the whole key pool while other models sit idle with quota to spare."""
        provider = _provider()
        tried: list[tuple[str, str]] = []

        async def attempt(client, model, degraded):
            tried.append((model, client))
            if model == "m1":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
            return "ok"

        result, model = asyncio.run(provider._execute(attempt, "m1"))
        assert result == "ok" and model == "m2"
        assert [m for m, _ in tried] == ["m1", "m2"]   # one shot at the limited model
        assert tried[0][1] != tried[1][1]              # ...and the key rotated too

    def test_every_model_is_used_before_giving_up(self):
        """The point of the chain: each model contributes its own quota."""
        provider = _provider(fallbacks=("m2", "m3"))
        tried: list[str] = []

        async def attempt(client, model, degraded):
            tried.append(model)
            if model != "m3":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
            return "ok"

        result, model = asyncio.run(provider._execute(attempt, "m1"))
        assert result == "ok" and model == "m3"
        assert tried == ["m1", "m2", "m3"]

    def test_a_parked_model_is_skipped_until_its_cooldown_expires(self):
        provider = _provider(fallbacks=("m2",), cooldown=30.0)
        provider._park("m1", "rate_limit")
        chosen, wait = provider._next_model(["m1", "m2"])
        assert chosen == "m2" and wait == 0.0

        provider._park("m2", "rate_limit")
        chosen, wait = provider._next_model(["m1", "m2"])
        assert chosen is None and 0 < wait <= 30.0   # all cooling -> wait, do not fail

    def test_overload_parks_the_model_without_rotating_the_key(self):
        """A 503 is the model's problem, not the key's."""
        provider = _provider()
        tried: list[tuple[str, str]] = []

        async def attempt(client, model, degraded):
            tried.append((model, client))
            if model == "m1":
                raise RuntimeError("503 Service Unavailable: overloaded")
            return "ok"

        result, model = asyncio.run(provider._execute(attempt, "m1"))
        assert result == "ok" and model == "m2"
        assert tried[0][1] == tried[1][1]  # same key, different model

    def test_invalid_keys_are_marked_dead_and_reported_precisely(self):
        """Every key rejected -> stop with the configuration diagnosis, not a
        generic "everything failed" after pointlessly trying every model."""
        provider = _provider()

        async def attempt(client, model, degraded):
            raise RuntimeError("API_KEY_INVALID")

        with pytest.raises(LLMProviderError, match="key.*are invalid"):
            asyncio.run(provider._execute(attempt, "m1"))

    def test_config_rejection_degrades_once_then_succeeds(self):
        provider = _provider(fallbacks=())
        modes: list[bool] = []

        async def attempt(client, model, degraded):
            modes.append(degraded)
            if not degraded:
                raise RuntimeError("400 INVALID_ARGUMENT: response_json_schema unsupported")
            return "ok"

        result, _ = asyncio.run(provider._execute(attempt, "m1"))
        assert result == "ok"
        assert modes == [False, True]  # full config, then the minimal one

    def test_every_path_exhausted_raises_a_provider_error(self):
        provider = _provider(fallbacks=())

        async def attempt(client, model, degraded):
            raise RuntimeError("500 internal error")

        with pytest.raises(LLMProviderError, match="last error"):
            asyncio.run(provider._execute(attempt, "m1"))


class TestOpenRouterTranslation:
    def test_tool_round_trip_shape(self):
        from bnbc.llm.openrouter import _to_openai

        call = ToolCall(id="c1", name="evaluate", args={"code": "x"})
        payload = _to_openai([
            system("s"), user("go"), assistant("", [call]), tool_result(call, "scorecard")
        ])
        assert [m["role"] for m in payload] == ["system", "user", "assistant", "tool"]
        assert payload[2]["tool_calls"][0]["function"]["name"] == "evaluate"
        assert payload[3]["tool_call_id"] == "c1"

    def test_gemini_models_use_tool_calling_for_schemas(self):
        from bnbc.llm.openrouter import _uses_tool_calling_for_schema

        assert _uses_tool_calling_for_schema("google/gemini-2.5-flash")
        assert not _uses_tool_calling_for_schema("deepseek/deepseek-v4-pro")


class TestJsonExtraction:
    def test_handles_fences_and_prose(self):
        assert extract_json('{"a": 1}') == {"a": 1}
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('Here you go:\n{"a": 1}\nhope that helps') == {"a": 1}

    def test_empty_and_unparseable_replies_raise(self):
        from bnbc.llm.base import LLMOutputError

        with pytest.raises(LLMOutputError):
            extract_json("")
        with pytest.raises(LLMOutputError):
            extract_json("no json here at all")


class TestGeminiTranslation:
    def test_messages_map_to_contents_and_system_instruction(self):
        pytest.importorskip("google.genai")
        from bnbc.llm.gemini import _to_gemini

        call = ToolCall(id="c1", name="evaluate", args={"code": "x"})
        instruction, contents = _to_gemini([
            system("s"), user("go"), assistant("", [call]), tool_result(call, "scorecard")
        ])
        assert instruction == "s"
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert contents[1].parts[0].function_call.name == "evaluate"
