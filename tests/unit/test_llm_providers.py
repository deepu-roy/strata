"""Provider-independent LLM configuration: settings, adapters, gateway, summary provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from code_kg.config import SummarySettings
from code_kg.domain.models import SummaryRequest
from code_kg.providers.llm.adapters import (
    _REGISTRY,
    OllamaAdapter,
    OpenAIAdapter,
    ProviderAdapter,
    provider_name,
    register_adapter,
    resolve_adapter,
)
from code_kg.providers.llm.gateway import LLMError, LLMGateway
from code_kg.providers.summary.base import build_summary_provider
from code_kg.providers.summary.llm import LLMSummaryProvider


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


# ── settings ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "model", "provider"),
    [
        ({}, "ollama/qwen2.5-coder:7b", "ollama"),  # the old default
        ({"model": "anthropic/claude-haiku-5-5"}, "anthropic/claude-haiku-5-5", "anthropic"),
        ({"model": "openai/gpt-4o-mini"}, "openai/gpt-4o-mini", "openai"),
        ({"model": "openrouter/anthropic/claude-3.5-haiku"}, "openrouter/anthropic/claude-3.5-haiku", "openrouter"),
        # legacy: provider + bare model
        ({"provider": "openai", "model": "gpt-4o-mini"}, "openai/gpt-4o-mini", "openai"),
        ({"provider": "ollama", "model": "qwen2.5-coder:14b"}, "ollama/qwen2.5-coder:14b", "ollama"),
        # a bare OpenAI id with no provider is recognised
        ({"model": "gpt-4o-mini"}, "openai/gpt-4o-mini", "openai"),
    ],
)  # fmt: skip
def test_model_string_selects_the_provider(kwargs: dict, model: str, provider: str) -> None:
    settings = SummarySettings(**kwargs)
    assert settings.model == model
    assert settings.provider == provider


def test_legacy_openai_compatible_base_url_gets_v1() -> None:
    """The old client appended /v1/chat/completions; LiteLLM wants /v1 in api_base."""
    legacy = SummarySettings(provider="openai", model="m", base_url="http://localhost:1234")
    assert legacy.base_url == "http://localhost:1234/v1"
    already = SummarySettings(model="openai/m", base_url="http://localhost:1234/v1/")
    assert already.base_url == "http://localhost:1234/v1"


def test_normalising_twice_is_stable() -> None:
    once = SummarySettings(provider="openai", model="gpt-4o-mini", base_url="http://h:1")
    again = SummarySettings(**once.model_dump())
    assert (again.model, again.provider, again.base_url) == (
        once.model,
        once.provider,
        once.base_url,
    )


# ── adapters ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "name"),
    [
        ("anthropic/claude-haiku-5-5", "anthropic"),
        ("openai/gpt-4o-mini", "openai"),
        ("azure/my-deployment", "openai"),
        ("openrouter/anthropic/claude-3.5-haiku", "openrouter"),
        ("ollama/qwen2.5-coder:7b", "ollama"),
        ("ollama_chat/llama3", "ollama"),
        ("bedrock/anthropic.claude-3", "passthrough"),
    ],
)
def test_adapter_is_resolved_from_the_prefix(model: str, name: str) -> None:
    assert provider_name(model) == name


def test_anthropic_drops_unsupported_params_and_json_response_format() -> None:
    settings = SummarySettings(model="anthropic/claude-haiku-5-5", api_key="k")
    opts = {"temperature": 0.0, "response_format": {"type": "json_object"}}
    final = resolve_adapter(settings.model).prepare(settings.model, opts, settings)
    assert final["drop_params"] is True
    assert "response_format" not in final
    assert final["api_key"] == "k"


def test_openai_keeps_json_mode_and_does_not_drop_params() -> None:
    settings = SummarySettings(model="openai/gpt-4o-mini")
    opts = {"response_format": {"type": "json_object"}}
    final = OpenAIAdapter().prepare(settings.model, opts, settings)
    assert final["response_format"] == {"type": "json_object"}
    assert "drop_params" not in final


def test_ollama_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUMMARY__BASE_URL, then LiteLLM's OLLAMA_API_BASE (set by compose), then localhost."""
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    default = SummarySettings(model="ollama/qwen")
    assert (
        OllamaAdapter().prepare(default.model, {}, default)["api_base"] == "http://localhost:11434"
    )
    custom = SummarySettings(model="ollama/qwen", base_url="http://gpu-box:11434")
    assert OllamaAdapter().prepare(custom.model, {}, custom)["api_base"] == "http://gpu-box:11434"

    monkeypatch.setenv("OLLAMA_API_BASE", "http://ollama:11434")
    assert OllamaAdapter().prepare(default.model, {}, default)["api_base"] == "http://ollama:11434"
    assert OllamaAdapter().prepare(custom.model, {}, custom)["api_base"] == "http://gpu-box:11434"


def test_extra_params_apply_but_the_callers_options_win() -> None:
    settings = SummarySettings(
        model="openrouter/x", extra_params={"top_p": 0.5, "temperature": 0.9}
    )
    final = resolve_adapter(settings.model).prepare(settings.model, {"temperature": 0.0}, settings)
    assert final["top_p"] == 0.5
    assert final["temperature"] == 0.0


def test_a_new_provider_is_one_class_and_a_registration() -> None:
    class AcmeAdapter:
        name = "acme"

        def matches(self, model: str) -> bool:
            return model.startswith("acme/")

        def prepare(self, model, opts, settings):  # type: ignore[no-untyped-def]
            return {**opts, "acme": True}

    adapter: ProviderAdapter = AcmeAdapter()
    register_adapter(adapter)
    try:
        assert provider_name("acme/model-1") == "acme"
    finally:
        _REGISTRY.remove(adapter)
    assert provider_name("acme/model-1") == "passthrough"


# ── gateway ───────────────────────────────────────────────────────────────────


async def test_gateway_returns_the_assistant_content_and_passes_prepared_options() -> None:
    completion = AsyncMock(return_value=_reply('{"ok": true}'))
    gateway = LLMGateway(SummarySettings(model="ollama/qwen", temperature=0.2), completion)

    out = await gateway.chat([{"role": "user", "content": "hi"}], json_mode=True)

    assert out == '{"ok": true}'
    kwargs = completion.await_args.kwargs
    assert kwargs["model"] == "ollama/qwen"
    assert kwargs["temperature"] == 0.2
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["api_base"] == "http://localhost:11434"


async def test_gateway_errors_name_the_model_provider_and_cause_class() -> None:
    class RateLimitError(Exception):
        pass

    completion = AsyncMock(side_effect=RateLimitError("quota"))
    gateway = LLMGateway(SummarySettings(model="anthropic/claude-haiku-5-5"), completion)

    with pytest.raises(LLMError) as exc:
        await gateway.chat([{"role": "user", "content": "hi"}])

    message = str(exc.value)
    assert "anthropic/claude-haiku-5-5" in message
    assert "(anthropic)" in message
    assert "RateLimitError" in message


async def test_gateway_treats_an_empty_reply_as_an_error() -> None:
    gateway = LLMGateway(SummarySettings(model="ollama/qwen"), AsyncMock(return_value=_reply("")))
    with pytest.raises(LLMError, match="empty completion"):
        await gateway.chat([{"role": "user", "content": "hi"}])


async def test_per_call_model_override_selects_that_models_adapter() -> None:
    completion = AsyncMock(return_value=_reply("x"))
    gateway = LLMGateway(SummarySettings(model="ollama/qwen"), completion)

    await gateway.chat(
        [{"role": "user", "content": "hi"}], model="anthropic/claude-haiku-5-5", json_mode=True
    )

    kwargs = completion.await_args.kwargs
    assert kwargs["model"] == "anthropic/claude-haiku-5-5"
    assert "response_format" not in kwargs  # Anthropic adapter, not Ollama's


# ── summary provider ──────────────────────────────────────────────────────────


def _request() -> SummaryRequest:
    return SummaryRequest(
        node_id="n1", node_type="Function", name="login", code_or_text="def login(): ..."
    )


@pytest.mark.parametrize(
    "model", ["ollama/qwen2.5-coder:7b", "anthropic/claude-haiku-5-5", "openai/gpt-4o-mini"]
)
async def test_switching_provider_is_configuration_only(model: str) -> None:
    """Same provider class and code path for every backend; only the model string differs."""
    completion = AsyncMock(
        return_value=_reply(
            '{"summary": "Logs a user in.", "tags": ["auth"], "complexity": "moderate"}'
        )
    )
    settings = SummarySettings(model=model)
    provider = build_summary_provider(settings)
    provider._gateway = LLMGateway(settings, completion)  # type: ignore[attr-defined]

    assert isinstance(provider, LLMSummaryProvider)
    response = await provider.summarise(_request())

    assert response.summary == "Logs a user in."
    assert completion.await_args.kwargs["model"] == model
    assert provider.name == model.split("/")[0]


async def test_a_provider_failure_falls_back_but_is_logged_with_the_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = SummarySettings(model="anthropic/claude-haiku-5-5")
    provider = LLMSummaryProvider(
        settings, LLMGateway(settings, AsyncMock(side_effect=ValueError("bad key")))
    )

    response = await provider.summarise(_request())

    assert response.summary  # heuristic fallback, the ingest keeps going
    assert any("anthropic" in r.message and "ValueError" in r.message for r in caplog.records)


def test_model_version_is_unchanged_for_existing_ollama_and_openai_setups() -> None:
    """Summaries are cached by model_version; the upgrade must not invalidate them."""
    assert (
        LLMSummaryProvider(SummarySettings(model="qwen2.5-coder:14b")).model_version
        == "qwen2.5-coder:14b"
    )
    assert (
        LLMSummaryProvider(SummarySettings(provider="openai", model="gpt-4o-mini")).model_version
        == "gpt-4o-mini"
    )
    # providers new to CodeKG keep the full string
    assert (
        LLMSummaryProvider(SummarySettings(model="anthropic/claude-haiku-5-5")).model_version
        == "anthropic/claude-haiku-5-5"
    )


async def test_a_stale_ollama_base_url_on_a_cloud_model_is_called_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Switching SUMMARY__MODEL to a cloud provider but keeping the Ollama URL is a trap."""
    settings = SummarySettings(model="anthropic/claude-haiku-5-5", base_url="http://ollama:11434")
    gateway = LLMGateway(settings, AsyncMock(return_value=_reply("x")))

    await gateway.chat([{"role": "user", "content": "hi"}])

    assert any("clear SUMMARY__BASE_URL" in r.message for r in caplog.records)
