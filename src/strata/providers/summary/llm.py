"""Summary provider backed by the provider-independent LLM gateway."""

import logging

from strata.config import SummarySettings
from strata.domain.models import SummaryRequest, SummaryResponse
from strata.providers.llm.adapters import provider_name
from strata.providers.llm.gateway import LLMGateway
from strata.providers.summary.prompts import build_prompt
from strata.providers.summary.response import fallback_response, parse_response

logger = logging.getLogger(__name__)

# Summaries cached before provider-independence were keyed by the bare model id for these two
# providers. Keep that key so upgrading does not invalidate (and re-pay for) every summary.
_LEGACY_UNPREFIXED = ("ollama/", "openai/")


class LLMSummaryProvider:
    """Generates node summaries with whatever provider ``SUMMARY__MODEL`` selects."""

    def __init__(self, settings: SummarySettings, gateway: LLMGateway | None = None) -> None:
        self._settings = settings
        self._gateway = gateway or LLMGateway(settings)
        self.name: str = provider_name(settings.model)

    @property
    def model_version(self) -> str:
        """Model identifier — used for cache invalidation when the model changes."""
        model = self._settings.model
        for prefix in _LEGACY_UNPREFIXED:
            if model.startswith(prefix):
                return model[len(prefix) :]
        return model

    async def summarise(self, request: SummaryRequest) -> SummaryResponse:
        """Generate a summary, tags and complexity for one code node.

        Failures degrade to a heuristic summary so one bad call does not stop an ingest, but
        they are logged with the provider and the cause's class so a bad key or an exhausted
        quota is visible rather than silently producing fallback summaries.
        """
        system_prompt, user_prompt = build_prompt(request)
        try:
            content = await self._gateway.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                json_mode=True,
            )
            return parse_response(content)
        except Exception as exc:  # noqa: BLE001 - one bad node must not stop an ingest
            logger.error(f"{self.name} summary failed for {request.name}: {exc}")
            return fallback_response(request)

    @property
    def usage(self) -> dict[str, int]:
        """Calls and tokens spent through this provider so far."""
        g = self._gateway
        return {
            "calls": g.calls,
            "prompt_tokens": g.prompt_tokens,
            "completion_tokens": g.completion_tokens,
        }

    async def check(self) -> None:
        """One tiny completion: proves the key, model and endpoint work before a paid batch.

        Raises:
            LLMError: With the provider's own reason (bad key, unknown model, no quota).
        """
        await self._gateway.chat([{"role": "user", "content": "Reply with the word OK."}])

    async def aclose(self) -> None:
        """Release gateway resources."""
        await self._gateway.aclose()
