# Configuration Reference

All settings are read from environment variables. Strata loads `.env` first, then
`.env.local` (which overrides `.env`). Use double-underscore `__` as the nested
key separator.

---

## Neo4j

| Variable | Default | Description |
|----------|---------|-------------|
| `NEO4J__URI` | *(required)* | Bolt URI — `bolt://localhost:7687` for local, `neo4j+s://xxxx.databases.neo4j.io` for Aura |
| `NEO4J__USER` | *(required)* | Database user |
| `NEO4J__PASSWORD` | *(required)* | Database password |
| `NEO4J__DATABASE` | `neo4j` | Database name. Leave as `neo4j` for Aura Free |

### Choosing a Neo4j tier

| Option | When to use |
|--------|-------------|
| **Local Docker** (`bolt://localhost:7687`) | Development, air-gapped environments |
| **Neo4j Aura Free** (cloud) | Zero-install start; free tier handles repos up to ~2 000 nodes |
| **Aura Professional / Enterprise** | Large repos (50 k+ nodes), production multi-tenant use |

### Minimum Neo4j version

**5.11** — required for the vector index used by semantic search. The fulltext and B-tree indexes work on any 5.x version.

---

## Embedding provider

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING__PROVIDER` | `sentence_transformers` | `sentence_transformers` (local) or `openai` (cloud) |
| `EMBEDDING__MODEL` | `BAAI/bge-small-en-v1.5` | Model name |
| `EMBEDDING__API_KEY` | *(empty)* | Required when `EMBEDDING__PROVIDER=openai` |
| `EMBEDDING__BASE_URL` | *(empty)* | Override for OpenAI-compatible endpoints (e.g. Azure, local proxies) |
| `EMBEDDING__BATCH_SIZE` | `32` | Texts per batch during bulk embedding |

### Supported embedding models

| Model | Provider | Dimensions | Notes |
|-------|----------|-----------|-------|
| `BAAI/bge-small-en-v1.5` | sentence_transformers | 384 | **Default.** Fast, small download (~120 MB), good quality |
| `BAAI/bge-base-en-v1.5` | sentence_transformers | 768 | Better recall, ~400 MB download |
| `BAAI/bge-large-en-v1.5` | sentence_transformers | 1024 | Highest quality local model, ~1.2 GB |
| `text-embedding-3-small` | openai | 1536 | Good balance of cost and quality |
| `text-embedding-3-large` | openai | 3072 | Highest quality, ~2× cost of small |

> **Important:** The vector index dimension is set at `bootstrap` time from `EMBEDDING__DIMENSIONS` (derived from the model). If you change the embedding model, drop and recreate the Neo4j vector index, then re-run `strata enrich` for all repos.

---

## Summary / LLM provider

The provider is **configuration, not code**. `SUMMARY__MODEL` is a [LiteLLM](https://docs.litellm.ai)
model string whose prefix selects the provider, so switching between Claude, OpenAI, OpenRouter,
Ollama or a local server is an `.env` change; there is nothing to install or edit. All LLM calls
(node summaries and documentation linking) go through one gateway, with small per-provider
adapters for the quirks (unsupported sampling parameters, JSON mode, default endpoints).

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMARY__MODEL` | `ollama/qwen2.5-coder:7b` | `<provider>/<model>`, see the table below |
| `SUMMARY__API_KEY` | *(empty)* | API key. Empty = LiteLLM reads the provider's own variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`) |
| `SUMMARY__BASE_URL` | *(empty)* | Endpoint override. Empty = the provider's default (Ollama: `OLLAMA_API_BASE`, else `http://localhost:11434`) |
| `SUMMARY__TEMPERATURE` | `0.1` | Lower = more deterministic outputs |
| `SUMMARY__TIMEOUT` | `120` | Per-request timeout in seconds |
| `SUMMARY__MAX_CONCURRENT` | `4` | Parallel LLM calls during enrichment |
| `SUMMARY__EXTRA_PARAMS` | `{}` | JSON merged into every completion (e.g. `{"top_p": 0.9}`) |

### Choosing a provider

| Provider | `SUMMARY__MODEL` example | Credentials |
|----------|--------------------------|-------------|
| Ollama (local) | `ollama/qwen2.5-coder:7b` | none |
| Claude (Anthropic) | `anthropic/claude-haiku-5-5` | `ANTHROPIC_API_KEY` or `SUMMARY__API_KEY` |
| OpenAI | `openai/gpt-4o-mini` | `OPENAI_API_KEY` or `SUMMARY__API_KEY` |
| OpenRouter | `openrouter/anthropic/claude-3.5-haiku` | `OPENROUTER_API_KEY` or `SUMMARY__API_KEY` |
| OpenAI-compatible server (LM Studio, llama.cpp, vLLM) | `openai/your-model` + `SUMMARY__BASE_URL=http://localhost:1234/v1` | any non-empty `SUMMARY__API_KEY` |
| Azure OpenAI | `azure/your-deployment` | see the LiteLLM Azure docs |
| Anything else LiteLLM supports | `bedrock/…`, `vertex_ai/…`, `groq/…` | per the LiteLLM docs |

**When you switch provider, clear `SUMMARY__BASE_URL`** unless you really want a custom endpoint.
A leftover Ollama URL would otherwise receive the cloud provider's requests (Strata logs a warning
when it sees this).

### Legacy configuration

The previous form still works and is converted on load: `SUMMARY__PROVIDER=openai` with a bare
`SUMMARY__MODEL=gpt-4o-mini` becomes `openai/gpt-4o-mini`; for `openai`, `/v1` is appended to
`SUMMARY__BASE_URL` if missing. Cached summaries are keyed by model; for existing Ollama and OpenAI
setups the key is unchanged, so upgrading does not trigger re-summarising.

### Recommended models

| Model string | Where it runs | RAM | Notes |
|--------------|---------------|-----|-------|
| `ollama/qwen2.5-coder:7b` | Ollama | ~8 GB | **Default.** Good quality, works on most machines |
| `ollama/qwen2.5-coder:14b` | Ollama | ~16 GB | Higher quality; needs ≥ 16 GB RAM |
| `ollama/deepseek-coder-v2:16b` | Ollama / LM Studio | ~20 GB | Strong alternative |
| `anthropic/claude-haiku-5-5` | Anthropic cloud | n/a | Fast and inexpensive |
| `openai/gpt-4o-mini` | OpenAI cloud | n/a | Fast, cheap, good code comprehension |

### Setup examples

**Ollama (default)**
```dotenv
SUMMARY__MODEL=ollama/qwen2.5-coder:7b
```

**Claude**
```dotenv
SUMMARY__MODEL=anthropic/claude-haiku-5-5
ANTHROPIC_API_KEY=sk-ant-...
```

**OpenAI**
```dotenv
SUMMARY__MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=sk-...
```

**OpenRouter**
```dotenv
SUMMARY__MODEL=openrouter/anthropic/claude-3.5-haiku
OPENROUTER_API_KEY=sk-or-...
```

**LM Studio / llama.cpp / vLLM** (any OpenAI-compatible server)
```dotenv
SUMMARY__MODEL=openai/your-model-name
SUMMARY__BASE_URL=http://localhost:1234/v1
SUMMARY__API_KEY=lm-studio
```

### Adding a provider

Most providers need nothing: LiteLLM's model strings already cover them. If one needs special
handling, add a small class to `strata/providers/llm/adapters.py` (`matches` + `prepare`) and call
`register_adapter(...)`; the gateway is untouched.

> Embeddings are configured separately (`EMBEDDING__*`, above). Claude has no embeddings API, so the
> embedding provider is independent of the summary provider.

### Docker: `.env` is the single source of truth

When running via Docker Compose, all environment variables are read from `.env`.
The `docker-compose.yml` file uses `${VAR:-default}` interpolation — there are no
hardcoded model names in the compose file. To switch the LLM model:

1. Edit `SUMMARY__MODEL` in `.env` (and the provider's API key if it is a cloud model; clear `SUMMARY__BASE_URL`)
2. Run `docker compose up -d` — the `ollama-init` service pulls the model before the MCP
   server starts when it is an Ollama model, and does nothing for a cloud model

> **Important:** If you change `EMBEDDING__MODEL` you must also drop and recreate the
> Neo4j vector index and re-run `strata enrich` for all repos (the vector index
> dimension is fixed at bootstrap time). LLM model changes (`SUMMARY__MODEL`) have
> no such constraint.

---

## MCP server

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | `stdio` (Claude Code / Claude Desktop) or `http` (Copilot, custom agents) |
| `MCP_HTTP_HOST` | `127.0.0.1` | Bind host for HTTP transport |
| `MCP_HTTP_PORT` | `8765` | Port for HTTP transport |

---

## Workdir (for remote repo ingestion)

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKDIR` | `/tmp/strata-repos` | Local directory where remote GitHub repos are cloned |
| `STRATA_GIT_TOKEN` | _(unset)_ | Access token for private HTTP(S) repos. Sent as an `Authorization: Basic` extra header through git's environment config; never placed in argv, URLs or logs |
| `STRATA_GIT_USER` | `x-access-token` | Username paired with the token (GitHub apps/PATs accept the default) |

Clones are stored as `WORKDIR/<repo-name>-<8 hex of sha1(normalised url)>`, so two different
repositories with the same name never share a directory. On every ingest the clone is fetched
and checked out (detached) at the requested `commit_sha`, or at `origin/<branch>`.

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Full `.env.example`

```dotenv
# ── Neo4j ─────────────────────────────────────────────────────────
NEO4J__URI=bolt://localhost:7687
NEO4J__USER=neo4j
NEO4J__PASSWORD=password123
NEO4J__DATABASE=neo4j

# ── Embeddings ────────────────────────────────────────────────────
EMBEDDING__PROVIDER=sentence_transformers
EMBEDDING__MODEL=BAAI/bge-small-en-v1.5
EMBEDDING__API_KEY=
EMBEDDING__BATCH_SIZE=32

# ── Summaries (LLM) ───────────────────────────────────────────────
SUMMARY__MODEL=ollama/qwen2.5-coder:7b   # or anthropic/claude-haiku-5-5, openai/gpt-4o-mini, ...
SUMMARY__BASE_URL=                       # empty = the provider's default endpoint
SUMMARY__API_KEY=                        # empty = read ANTHROPIC_API_KEY / OPENAI_API_KEY / ...
SUMMARY__TEMPERATURE=0.1
SUMMARY__TIMEOUT=120
SUMMARY__MAX_CONCURRENT=4

# ── MCP server ────────────────────────────────────────────────────
MCP_TRANSPORT=stdio
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8765

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL=INFO

# ── Git workdir (for remote repo ingestion) ───────────────────────
WORKDIR=/tmp/strata-repos
# STRATA_GIT_TOKEN=...   # private repos (HTTP/HTTPS)
# STRATA_GIT_USER=x-access-token
```

---

## Performance tuning

### Enrichment throughput

`SUMMARY__MAX_CONCURRENT` controls how many parallel LLM calls run during `strata enrich`. Rule of thumb:

- Local CPU inference (Ollama, llama.cpp, LM Studio on CPU): **`1`** — most local servers process one request at a time; higher concurrency causes queued requests to reset, producing spurious retry warnings
- Local GPU inference: `4–8`
- Cloud APIs (OpenAI, Azure, Groq, etc.): `8–16` (watch rate limits)

### Batch embedding

`EMBEDDING__BATCH_SIZE` controls how many texts are embedded in a single model call. Larger batches are more efficient but use more RAM. Default (`32`) is safe for most hardware.

### Large repos

For repos with > 5 000 nodes, run enrichment in incremental batches:

```bash
# Process 500 nodes per run, repeat until done
strata enrich --repo my-service --limit 500
```

Each run picks up where the last left off (only unenriched nodes are processed).
