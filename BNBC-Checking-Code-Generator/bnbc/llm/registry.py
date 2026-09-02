"""Provider selection.

``LLM_PROVIDER`` picks the backend once per process; tests install a fake
through :func:`set_provider_for_testing`. Providers are constructed lazily so
importing the pipeline never requires an API key.
"""

from __future__ import annotations

import logging

from bnbc import config as cfg
from bnbc.llm.base import LLMProvider, LLMProviderError

logger = logging.getLogger("bnbc.llm.registry")

_provider: LLMProvider | None = None
_override: LLMProvider | None = None


def build_provider(name: str) -> LLMProvider:
    """Construct the backend called ``name``."""
    if name == "gemini":
        from bnbc.llm.gemini import GeminiProvider

        return GeminiProvider()
    if name == "openrouter":
        from bnbc.llm.openrouter import OpenRouterProvider

        return OpenRouterProvider()
    raise LLMProviderError(
        f"unknown LLM_PROVIDER {name!r} — supported: 'gemini', 'openrouter'"
    )


def get_provider() -> LLMProvider:
    """The active backend (cached; test override wins)."""
    global _provider
    if _override is not None:
        return _override
    if _provider is None:
        _provider = build_provider(cfg.LLM_PROVIDER)
        logger.info("LLM provider: %s", _provider.name)
    return _provider


def set_provider_for_testing(provider: LLMProvider) -> None:
    """Install a fake backend. Tests only."""
    global _override
    _override = provider


def reset_provider() -> None:
    """Drop the test override and the cached backend."""
    global _override, _provider
    _override = None
    _provider = None
