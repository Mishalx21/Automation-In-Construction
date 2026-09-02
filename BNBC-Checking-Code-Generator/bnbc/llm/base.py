"""Provider-neutral LLM contract.

Everything above this module (pipeline nodes, prompts, the agentic tool loop)
speaks only the types defined here — :class:`Message`, :class:`ToolSpec`,
:class:`ToolCall`, :class:`LLMResponse`. A concrete backend implements
:class:`LLMProvider` and is selected at runtime (``bnbc.llm.registry``), so
re-pointing the whole pipeline at another vendor is one class plus one env
var, never a change in a node.

Error taxonomy: every failure that originates in the LLM stack (transport,
provider, unparseable output, wall-clock ceiling, exhausted budget) is an
:class:`LLMError`. Providers MUST wrap vendor exceptions in one of these —
the graph guard classifies terminal-but-expected failures by type, and an
unwrapped vendor exception would be treated as a programming bug and crash
the run.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Base of every failure raised by the LLM layer.

    Two distinct accountings ride along:

    * ``call_usage`` — tokens the *failed attempt itself* burned, when the
      provider could recover them (a reply that arrived but did not parse
      still costs money). ``call_llm`` meters it before propagating.
    * ``usage`` — the whole meter snapshot, attached by ``call_llm`` so a
      terminal path can persist what the rule cost (observed live: a $0.10
      rejection once reported "0 tokens").
    """

    def __init__(
        self, message: str, *, usage: dict | None = None, call_usage: dict | None = None
    ):
        super().__init__(message)
        self.usage: dict = dict(usage or {})
        self.call_usage: dict = dict(call_usage or {})


class LLMProviderError(LLMError):
    """Transport/provider failure after the provider exhausted its retries."""


class LLMOutputError(LLMError):
    """The model replied, but the reply could not be parsed/validated."""


class LLMWallTimeout(LLMOutputError):
    """A call exceeded the wall-clock ceiling.

    Subclasses :class:`LLMOutputError` so the graph guard treats it as an
    infrastructure failure; ``call_llm`` never retries it (the retry would
    take just as long).
    """


class BudgetExceeded(LLMError):
    """The per-rule token budget is exhausted. Never retried, never masked."""


def is_llm_infra_error(exc: BaseException) -> bool:
    """True for LLM-stack failures the graph may convert into a rejection.

    ``BudgetExceeded`` is excluded — it has its own terminal handling.
    """
    return isinstance(exc, LLMError) and not isinstance(exc, BudgetExceeded)


# ---------------------------------------------------------------------------
# Messages / tools / responses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Message:
    """One conversation turn.

    ``tool_calls`` is only meaningful on an ``assistant`` message;
    ``tool_call_id``/``tool_name`` only on a ``tool`` message (vendors key
    tool results either by id or by name, so both are carried).
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    tool_name: str = ""


def system(content: str) -> Message:
    return Message(role="system", content=content)


def user(content: str) -> Message:
    return Message(role="user", content=content)


def assistant(content: str = "", tool_calls: Sequence[ToolCall] = ()) -> Message:
    return Message(role="assistant", content=content, tool_calls=tuple(tool_calls))


def tool_result(call: ToolCall, content: str) -> Message:
    return Message(
        role="tool", content=content, tool_call_id=call.id, tool_name=call.name
    )


@dataclass(frozen=True)
class Usage:
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class LLMResponse:
    """One model reply, normalised across providers."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    parsed: Any = None
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def as_message(self) -> Message:
        """The reply as an ``assistant`` turn, ready to append to a history."""
        return assistant(self.text, self.tool_calls)


# ---------------------------------------------------------------------------
# JSON helpers (shared by every provider)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> Any:
    """Parse JSON out of a model reply, tolerating fences and prose.

    Raises :class:`LLMOutputError` when nothing parseable is present.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise LLMOutputError("empty reply — no JSON to parse")

    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        if newline != -1:
            cleaned = cleaned[newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMOutputError(f"no valid JSON in reply (first 300 chars): {text[:300]!r}")


def validate_structured(schema: type[BaseModel], text: str) -> BaseModel:
    """Parse ``text`` as JSON and validate it against ``schema``."""
    payload = extract_json(text)
    try:
        return schema.model_validate(payload)
    except Exception as exc:
        raise LLMOutputError(f"reply did not validate against {schema.__name__}: {exc}")


def json_schema_of(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a pydantic model (with ``$defs`` for nested models)."""
    return schema.model_json_schema()


def schema_instruction(schema: type[BaseModel]) -> str:
    """Prompt text pinning the output to ``schema``.

    Used by the schema-less degradation path: when a provider rejects a
    native structured-output request (schema dialect it does not accept), the
    contract is restated in the prompt and the reply is validated locally.
    """
    return (
        "Reply with a single JSON object and nothing else — no prose, no code "
        "fences. It must validate against this JSON Schema:\n"
        + json.dumps(json_schema_of(schema), ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """A backend able to complete a conversation.

    Implementations own their own resilience (key pools, key/model rotation,
    provider retries) and surface a single normalised reply. ``structured``
    and ``tools`` are mutually exclusive — no caller needs both, and every
    vendor models them as the same underlying mechanism.
    """

    #: Short identifier used in logs and in the model-defaults table.
    name: str = "llm"

    def preflight(self) -> None:
        """Fail now on anything the first real call would reject.

        Callers run this before doing expensive work — materialising a rule's
        fixtures takes minutes, and discovering a missing SDK or key
        afterwards wastes all of it. Raise an :class:`LLMError` with a fix.
        """

    @abstractmethod
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
        """Complete ``messages``; raise an :class:`LLMError` on failure."""
