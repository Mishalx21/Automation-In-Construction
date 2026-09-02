"""Provider-agnostic LLM layer.

``bnbc.llm`` is the pipeline's only door to a model. Nodes import the message
helpers, :func:`call_llm` and :class:`TokenMeter` from here; which vendor
answers is decided by ``LLM_PROVIDER`` in ``bnbc.llm.registry``.
"""

from bnbc.llm.base import (
    BudgetExceeded,
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMWallTimeout,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    assistant,
    extract_json,
    is_llm_infra_error,
    system,
    tool_result,
    user,
)
from bnbc.llm.call import call_llm
from bnbc.llm.metering import TokenMeter, call_cost_usd
from bnbc.llm.registry import get_provider, reset_provider, set_provider_for_testing

__all__ = [
    "BudgetExceeded",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "LLMWallTimeout",
    "Message",
    "ToolCall",
    "ToolSpec",
    "TokenMeter",
    "Usage",
    "assistant",
    "call_cost_usd",
    "call_llm",
    "extract_json",
    "get_provider",
    "is_llm_infra_error",
    "reset_provider",
    "set_provider_for_testing",
    "system",
    "tool_result",
    "user",
]
