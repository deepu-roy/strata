# CodeKG graph contract

Machine-readable source of truth: [`contract/graph-contract.json`](../contract/graph-contract.json)
(version **1.0.0**). CodeKG is the producer; consumers (e.g. KDTIP) may rely on everything
in that file and on nothing else. Drift is caught by `tests/contract/` (no Neo4j) and
`tests/integration/test_graph_contract_integration.py` (real Neo4j via testcontainers).

Versioning is semver: MAJOR for removed/renamed labels, edge types, required properties,
id formats, MCP tools/params/result fields; MINOR for additions; PATCH for doc fixes.
Changing the code without updating the contract fails CI.

## Bringing up a graph

```bash
docker run -d --name codekg-neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/<pw> neo4j:5.15   # or compose
export NEO4J__URI=bolt://localhost:7687 NEO4J__USER=neo4j NEO4J__PASSWORD=<pw>
code-kg bootstrap                                  # constraints + indexes (idempotent)
code-kg ingest /path/to/repo --repo my-repo \
    --pattern "**/*.ts,**/*.cs,**/*.py,**/*.java,**/*.go,**/*.md"
code-kg mcp http --port 8765                       # optional: MCP over streamable HTTP
```

The default `--pattern` covers only ts/tsx/js/jsx/cs/md; pass `.py`, `.java`, `.go`
explicitly. Ingestion needs no embedding model or LLM (`code-kg enrich` adds
`summary`/`tags`/`summary_embedding` separately). Everything is scoped by the `repo`
property, so `code-kg`/`delete_repo` for one repo never touches another.

## Node labels and ids

Every code node has its primary label plus `CodeNode`; doc nodes have `DocNode`.

| Label | Id format | Emitted today |
|---|---|---|
| Repo | `repo:<repo>` | yes |
| File | `file:<repo>:<path>` | yes |
| Class | `class:<repo>:<path>:<Name>` | yes |
| Interface | `interface:<repo>:<path>:<Name>` | yes (C#, Java, Go; not TS) |
| Function | `function:<repo>:<path>:<name>:<sig4>` | yes (also class methods) |
| Method | `method:<repo>:<path>:<Class>:<name>:<sig4>` | no (declared only) |
| Enum, Endpoint, Concept | see JSON | no (declared only) |
| Document | `doc:<repo>:<path>` | yes |
| Section | `doc:<repo>:<path>#<heading-slug>` | yes |
| Layer | `layer:<repo>:<layer-name>` | yes (not a CodeNode) |
| Module | `module:<repo>:<import-path>` | yes, external placeholder (not a CodeNode) |

Ids are opaque strings; do not parse them, use properties.

Required on all CodeNode/DocNode: `id, name, nameTokens, repo, filePath, language, signature,
signatureHash, fileHash, created_at, updated_at`. Optional: `lineRange` (`[start,end]`),
`layer` (CodeNode only; absent when undetected), `summary, tags, complexity, summary_embedding`
(after `enrich`), `last_seen_commit, deleted_at` (soft delete; filter `deleted_at IS NULL`).
Properties are camelCase except the snake_case ones listed; **the path property is `filePath`**.
MCP results use `file_path` / `line_range`.

### `repo`

The ingest slug verbatim (case preserved). CLI default: directory name (parent name for
`src/lib/app/source/main/code`). `Repo` nodes expose it as `name`. Consumers must normalise
before comparing: last `/` segment, lower-case, strip trailing `.git`.

### `filePath`

Relative to the ingested root, POSIX separators, no leading `./` or `/`. Empty on Repo/Layer/Module.

## Edge types

Emitted by `ingest`: `IN_REPO` (CodeNode|DocNode -> Repo), `BELONGS_TO_LAYER` (CodeNode -> Layer),
`IMPORTS` (File -> File|Module), `CALLS` (Function -> Function, resolved only), `INHERITS`
(Class -> Class), `IMPLEMENTS` (Class -> Interface), `HAS_SECTION` (Document -> Section),
`LINKS_TO` (Document -> Document). After `code-kg test-map`: `TESTS` (test -> prod, `weight`).
After `link-docs`: `DOCUMENTS` (Section -> CodeNode, `weight`). On reindex: `RENAMED_FROM`
(old File -> new File). Declared but never written: `MENTIONS`, `INSTANTIATES`, `CONTAINS`,
`DEFINES`, `EXTENDS`, `REFERENCES`, `EXPOSES_ENDPOINT`, `CALLS_HTTP`.

## Constraints and indexes (created by `code-kg bootstrap`)

Uniqueness on `<Label>.id` for File, Class, Interface, Function, Method, Endpoint, Document,
Section, Layer, Concept, Repo (names `file_id`, `class_id`, ...). Range indexes
`node_repo`, `node_filepath`, `node_last_seen` (CodeNode), `doc_repo` (DocNode). Full-text
`code_search`, `doc_search`. Vector `code_embeddings`, `doc_embeddings` (cosine, dimension from
`EMBEDDING__MODEL`, default 384).

## MCP tools

Stable (consumers may rely on): `find_nodes`, `get_node`, `impact_analysis`, `trace_call_chain`,
`find_tests_for`, `find_docs_for`, `find_code_for`, `list_projects`, `list_layers`,
`get_architecture`. Other tools (`semantic_search`, `get_diff`, `find_dead_code`, and the
write tools) are listed in the JSON as `internal`. Params, defaults and result field names
are in the JSON and verified against the live FastMCP schema and pydantic models.
Default HTTP port 8765 (`MCP_HTTP_PORT` or `code-kg mcp http --port N`), endpoint `/mcp`,
plus `GET /api/repo-state?slug=`.

## Known ambiguities

See `known_ambiguities` in the JSON (property casing, Function vs Method, Enum id prefix,
Section/Document prefix sharing, never-emitted edges, case-insensitive repo handling only in
the CLI, `RENAMED_FROM` direction, embedding-dimension fallbacks, unlabelled id lookups).

## Shared-Neo4j hazards (producer-side)

- Labels are unqualified (`File`, `Class`, `Document`, `Section`, `Layer`, `Repo`, `Module`,
  `Concept`) and constraint/index names are global (`file_id`, `repo_id`, `code_search`...).
  An app sharing the database must avoid these labels/names, or use a separate database
  (`NEO4J__DATABASE`; Neo4j Community has a single database, so use a second instance).
- `IN_REPO`/`BELONGS_TO_LAYER` creation and `get_node`/`impact_analysis` use unlabelled
  `MATCH (n {id: ...})` (full scan; matches any node with that `id`).
- Migration data-fixes touch every `CodeNode` regardless of repo.
- Vector indexes are created once; changing `EMBEDDING__MODEL` to a different dimension does
  not recreate them.
- CodeKG's `docker-compose.yml` publishes Neo4j on 7474/7687 and MCP on 8765.
