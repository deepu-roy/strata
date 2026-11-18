"""Provider adapters: isolate per-provider quirks behind the LLM gateway.

The provider is *configuration*: ``SUMMARY__MODEL`` is a LiteLLM model string
(``anthropic/...``, ``openai/...``, ``openrouter/...``, ``ollama/...``) and the matching
:class:`ProviderAdapter` is picked from its prefix. An adapter owns what differs between
providers (unsupported sampling parameters, JSON-mode support, base URLs) so callers never
branch on a provider.

Adding a provider is one small class plus :func:`register_adapter`; no gateway edit. This
module never imports a provider SDK (only :mod:`strata.providers.llm.gateway` talks to
``litellm``). Same design as Assay's ``assay.core.llm.providers``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

from strata.config import SummarySettings

__all__ = [
    "AnthropicAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
    "OpenRouterAdapter",
    "PassthroughAdapter",
    "ProviderAdapter",
    "provider_name",
    "register_adapter",
    "resolve_adapter",
]

_OLLAMA_DEFAULT_BASE = "http://localhost:11434"


class ProviderAdapter(Protocol):
    """Adapts one provider's request quirks. Stateless; selected per model string."""

    name: str

    def matches(self, model: str) -> bool:
        """Return True if this adapter handles the LiteLLM ``model`` string."""
        ...

    def prepare(
        self, model: str, opts: Mapping[str, Any], settings: SummarySettings
    ) -> dict[str, Any]:
        """Return the final completion kwargs for ``model``.

        Caller-supplied ``opts`` always win over adapter defaults.
        """
        ...


def _with_defaults(
    opts: Mapping[str, Any],
    settings: SummarySettings,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge adapter defaults, configured endpoint/key and extras under the caller's opts."""
    merged: dict[str, Any] = {**(defaults or {})}
    if settings.base_url:
        merged["api_base"] = settings.base_url
    if settings.api_key:
        merged["api_key"] = settings.api_key
    merged.update(settings.extra_params)
    merged.update(opts)
    return merged


class _PrefixAdapter:
    """Base: matches ``<prefix>/...`` model strings and drops params a model rejects."""

    name = ""
    prefixes: tuple[str, ...] = ()

    def matches(self, model: str) -> bool:
        """Match on the provider prefix before the first ``/``."""
        return model.split("/", 1)[0].lower() in self.prefixes

    def prepare(
        self, model: str, opts: Mapping[str, Any], settings: SummarySettings
    ) -> dict[str, Any]:
        """Default behaviour: drop unsupported params instead of failing the call."""
        return _with_defaults(opts, settings, {"drop_params": True})


class AnthropicAdapter(_PrefixAdapter):
    """Anthropic.

    Some models accept only default sampling (e.g. temperature=1), so unsupported params are
    dropped rather than failing the call. JSON mode is not requested through
    ``response_format`` (Anthropic has no native switch); the prompts ask for JSON and the
    response parser tolerates code fences.
    """

    name = "anthropic"
    prefixes = ("anthropic",)

    def prepare(
        self, model: str, opts: Mapping[str, Any], settings: SummarySettings
    ) -> dict[str, Any]:
        """Drop unsupported params and the OpenAI-style ``response_format``."""
        merged = _with_defaults(opts, settings, {"drop_params": True})
        merged.pop("response_format", None)
        return merged


class OpenAIAdapter(_PrefixAdapter):
    """OpenAI / Azure OpenAI / any OpenAI-compatible server (set ``SUMMARY__BASE_URL``)."""

    name = "openai"
    prefixes = ("openai", "azure")

    def prepare(
        self, model: str, opts: Mapping[str, Any], settings: SummarySettings
    ) -> dict[str, Any]:
        """OpenAI accepts the options as-is (including JSON mode); add configured extras."""
        return _with_defaults(opts, settings)


class OpenRouterAdapter(_PrefixAdapter):
    """OpenRouter: one endpoint, many upstream models, so unsupported params are dropped."""

    name = "openrouter"
    prefixes = ("openrouter",)


class OllamaAdapter(_PrefixAdapter):
    """Local Ollama.

    Base URL precedence: ``SUMMARY__BASE_URL``, then LiteLLM's standard ``OLLAMA_API_BASE``
    (what the Docker compose stack sets), then ``http://localhost:11434``.
    """

    name = "ollama"
    prefixes = ("ollama", "ollama_chat")

    def prepare(
        self, model: str, opts: Mapping[str, Any], settings: SummarySettings
    ) -> dict[str, Any]:
        """Point at the configured Ollama server and drop unsupported params."""
        default_base = os.environ.get("OLLAMA_API_BASE") or _OLLAMA_DEFAULT_BASE
        return _with_defaults(opts, settings, {"drop_params": True, "api_base": default_base})


class PassthroughAdapter(_PrefixAdapter):
    """Fallback for any other LiteLLM provider (Bedrock, Vertex, vLLM, ...)."""

    name = "passthrough"

    def matches(self, model: str) -> bool:
        """Always matches; it is the last resort."""
        return True


_FALLBACK = PassthroughAdapter()
_REGISTRY: list[ProviderAdapter] = [
    AnthropicAdapter(),
    OpenAIAdapter(),
    OpenRouterAdapter(),
    OllamaAdapter(),
]


def register_adapter(adapter: ProviderAdapter, *, first: bool = True) -> None:
    """Add an adapter for a new provider (checked before the built-ins by default)."""
    if first:
        _REGISTRY.insert(0, adapter)
    else:
        _REGISTRY.append(adapter)


def resolve_adapter(model: str) -> ProviderAdapter:
    """Return the adapter for a LiteLLM model string; the passthrough adapter if none match."""
    for adapter in _REGISTRY:
        if adapter.matches(model):
            return adapter
    return _FALLBACK


def provider_name(model: str) -> str:
    """Short provider label for logs and error messages (e.g. ``anthropic``)."""
    return resolve_adapter(model).name
