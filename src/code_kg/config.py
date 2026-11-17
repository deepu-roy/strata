"""Configuration management via Pydantic Settings."""

from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Neo4jSettings(BaseSettings):
    """Neo4j connection settings."""

    uri: str
    user: str
    password: str
    database: str = "neo4j"


class EmbeddingSettings(BaseSettings):
    """Embedding provider configuration."""

    provider: Literal["sentence_transformers", "openai"] = "sentence_transformers"
    model: str = "BAAI/bge-small-en-v1.5"
    api_key: str | None = None
    batch_size: int = 32

    @property
    def dimensions(self) -> int:
        """Return the embedding dimensions for the configured model."""
        dims = {
            "BAAI/bge-small-en-v1.5": 384,
            "BAAI/bge-base-en-v1.5": 768,
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
        }
        return dims.get(self.model, 384)


# Provider prefixes LiteLLM routes on (``<prefix>/<model>``). A model without one of these is
# the legacy bare form (``qwen2.5-coder:7b``) and is prefixed from ``provider``.
_PROVIDER_PREFIXES = frozenset(
    {
        "anthropic", "openai", "azure", "openrouter", "ollama", "ollama_chat", "bedrock",
        "vertex_ai", "gemini", "groq", "mistral", "together_ai", "deepseek", "xai", "cohere",
        "huggingface", "hosted_vllm", "lm_studio",
    }
)  # fmt: skip


class SummarySettings(BaseSettings):
    """Summary/LLM configuration. The provider is configuration, not code.

    ``model`` is a LiteLLM model string whose prefix selects the provider, e.g.
    ``ollama/qwen2.5-coder:7b``, ``anthropic/claude-haiku-5-5``, ``openai/gpt-4o-mini``,
    ``openrouter/anthropic/claude-3.5-haiku``. Switching provider is a config change only.

    Legacy form still accepted: ``provider`` + a bare ``model`` (``SUMMARY__PROVIDER=openai``,
    ``SUMMARY__MODEL=gpt-4o-mini``) is folded into the prefixed form on load.
    """

    provider: str | None = None  # derived from ``model``'s prefix; legacy input only
    model: str = "qwen2.5-coder:7b"
    base_url: str = ""  # api_base override; empty = the provider's default
    api_key: str | None = None  # empty = LiteLLM reads the provider's own env var
    temperature: float = 0.1
    max_concurrent: int = 4
    timeout: float = 120.0
    extra_params: dict[str, Any] = {}  # JSON in env, merged into every completion

    @model_validator(mode="after")
    def _normalise_model(self) -> "SummarySettings":
        head = self.model.split("/", 1)[0].lower()
        bare_openai = "/" not in self.model and (
            self.model.startswith("gpt-") or (self.model[:1] == "o" and self.model[1:2].isdigit())
        )
        if head not in _PROVIDER_PREFIXES:
            # Legacy / bare model: the provider comes from ``provider`` (default: Ollama).
            prefix = "openai" if bare_openai and not self.provider else (self.provider or "ollama")
            self.model = f"{prefix}/{self.model}"
        self.provider = self.model.split("/", 1)[0].lower()
        # Legacy OpenAI-compatible servers were configured without ``/v1`` (the old client
        # appended ``/v1/chat/completions``); LiteLLM expects it in ``api_base``.
        if self.provider == "openai" and self.base_url:
            base = self.base_url.rstrip("/")
            self.base_url = base if base.endswith("/v1") else f"{base}/v1"
        return self


class Settings(BaseSettings):
    """Root application settings, loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    neo4j: Neo4jSettings
    embedding: EmbeddingSettings = EmbeddingSettings()
    summary: SummarySettings = SummarySettings()
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8765
    log_level: str = "INFO"
    # Directory used for git clones by the ingest_repo write tool
    workdir: str = "./workdir"
