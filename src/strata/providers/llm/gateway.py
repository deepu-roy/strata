"""The single doorway to an LLM: provider-independent chat completions via LiteLLM.

Replaces the former hand-rolled HTTP client (which spoke only Ollama's and OpenAI's wire
formats). Callers pass messages; the provider, endpoint and quirks come from configuration
(:class:`~strata.config.SummarySettings`) and :mod:`strata.providers.llm.adapters`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from strata.config import SummarySettings
from strata.providers.llm.adapters import resolve_adapter

__all__ = ["LLMError", "LLMGateway"]

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

CompletionFn = Callable[..., Awaitable[Any]]


class LLMError(RuntimeError):
    """A completion failed. The message names the model, provider and the cause's class."""


async def _litellm_acompletion(*, model: str, messages: list[dict[str, str]], **opts: Any) -> Any:
    """Default completion callable: lazily delegates to ``litellm.acompletion``."""
    import litellm

    return await litellm.acompletion(model=model, messages=messages, **opts)


class LLMGateway:
    """Async chat completions for whichever provider ``settings.model`` selects.

    Args:
        settings: Summary/LLM configuration (model string, endpoint, key, extras).
        completion_fn: Injectable completion callable (tests); defaults to LiteLLM.
    """

    def __init__(
        self, settings: SummarySettings, completion_fn: CompletionFn | None = None
    ) -> None:
        self._settings = settings
        self._completion_fn: CompletionFn = completion_fn or _litellm_acompletion

    @property
    def model(self) -> str:
        """The configured LiteLLM model string."""
        return self._settings.model

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """Return the assistant message content.

        Args:
            messages: ``{role, content}`` dicts.
            model: Override the configured model (LiteLLM string).
            temperature: Override the configured temperature.
            json_mode: Ask for a JSON object where the provider supports it; adapters drop it
                where it does not (the parser tolerates fenced JSON either way).

        Raises:
            LLMError: On any provider failure or an empty reply.
        """
        target = model or self._settings.model
        adapter = resolve_adapter(target)
        opts: dict[str, Any] = {
            "temperature": self._settings.temperature if temperature is None else temperature,
            "timeout": self._settings.timeout,
            "num_retries": _MAX_RETRIES,
        }
        if json_mode:
            opts["response_format"] = {"type": "json_object"}
        final = adapter.prepare(target, opts, self._settings)
        base = str(final.get("api_base") or "")
        if adapter.name != "ollama" and ":11434" in base:
            logger.warning(
                f"model {target!r} ({adapter.name}) will be sent to {base}, which looks like an "
                "Ollama endpoint; if you switched provider, clear SUMMARY__BASE_URL"
            )

        try:
            response = await self._completion_fn(model=target, messages=messages, **final)
        except Exception as exc:
            # The cause's class matters: some errors (e.g. RemoteProtocolError) have an empty
            # str(), and "RateLimitError" vs "AuthenticationError" is what the operator needs.
            raise LLMError(
                f"completion failed for model {target!r} ({adapter.name}): "
                f"{type(exc).__name__}: {exc or '(no message)'}"
            ) from exc

        content = _first_content(response)
        if not content:
            raise LLMError(f"empty completion from model {target!r} ({adapter.name})")
        return content

    async def aclose(self) -> None:
        """No persistent connection to close (kept for provider lifecycle symmetry)."""


def _first_content(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, TypeError):
        return ""
