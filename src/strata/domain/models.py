"""Domain models for strata."""

from typing import Any, Literal

from pydantic import BaseModel, Field

# === AST Extraction (Raw) ===


class RawNode(BaseModel):
    """Raw node extracted by an IngestionSource, pre-normalization."""

    type: Literal[
        "file",
        "module",
        "class",
        "interface",
        "function",
        "method",
        "enum",
        "endpoint",
        "document",
        "section",
    ]
    name: str
    language: str | None = None
    file_path: str
    repo: str
    line_range: tuple[int, int] | None = None
    signature: str | None = None
    code_snippet: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RawEdge(BaseModel):
    """Raw edge extracted by an IngestionSource, pre-normalization."""

    type: str
    from_id: str
    to_id: str
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


# === Normalized (post ID assignment) ===


class NormalizedNode(BaseModel):
    """Node after stable ID assignment and enrichment."""

    id: str
    type: str
    name: str
    repo: str
    language: str | None = None
    file_path: str | None = None
    line_range: tuple[int, int] | None = None
    signature: str | None = None
    signature_hash: str | None = None
    file_hash: str | None = None
    code_snippet: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary_embedding: list[float] | None = None
    complexity: str | None = None
    layer: str | None = None
    first_seen_commit: str | None = None
    last_seen_commit: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class NormalizedEdge(BaseModel):
    """Edge after normalization."""

    id: str | None = None
    type: str
    from_id: str
    to_id: str
    weight: float = 1.0
    unresolved: bool = False
    first_seen_commit: str | None = None
    last_seen_commit: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# === Enrichment Requests/Responses ===


class SummaryRequest(BaseModel):
    """Request to generate a summary for a node."""

    node_id: str
    node_type: str
    name: str
    language: str | None = None
    code_or_text: str
    context: str | None = None


class SummaryResponse(BaseModel):
    """Response from summary provider."""

    summary: str
    tags: list[str] = Field(default_factory=list)
    complexity: Literal["simple", "moderate", "complex"] = "moderate"


# === MCP Tool I/O Models ===


class FoundNode(BaseModel):
    """A found node in search results."""

    id: str
    type: str
    name: str
    file_path: str | None = None
    summary: str
    tags: list[str] = Field(default_factory=list)
    layer: str | None = None
    score: float | None = None  # present in search results; None in structural queries


class NeighborRef(BaseModel):
    """Reference to a neighboring node."""

    id: str
    type: str
    name: str
    edge_type: str
    edge_weight: float


class FindNodesInput(BaseModel):
    """Input for find_nodes tool."""

    query: str
    types: list[str] | None = None
    repos: list[str] | None = None
    layers: list[str] | None = None
    limit: int = 10
    use_semantic: bool = True


class FindNodesOutput(BaseModel):
    """Output for find_nodes tool."""

    nodes: list[FoundNode]
    total_matched: int


class GetNodeInput(BaseModel):
    """Input for get_node tool."""

    id: str
    include_neighbors: bool = True
    neighbor_edge_types: list[str] | None = None
    max_neighbors: int = 20


class NodeDetail(BaseModel):
    """Detailed view of a node."""

    id: str
    type: str
    name: str
    summary: str
    tags: list[str]
    layer: str | None = None
    file_path: str | None = None
    line_range: tuple[int, int] | None = None
    signature: str | None = None
    # code_snippet removed — code is not stored in the graph. Use file_path +
    # line_range to read source directly (avoids duplicating data in Neo4j).
    neighbors: list[NeighborRef] = Field(default_factory=list)


class ImpactAnalysisInput(BaseModel):
    """Input for impact_analysis tool."""

    id: str
    max_depth: int = 3
    include_tests: bool = True
    include_docs: bool = True


class ImpactPath(BaseModel):
    """A path in impact analysis."""

    node: FoundNode
    depth: int
    path_ids: list[str]


class ImpactAnalysisOutput(BaseModel):
    """Output for impact_analysis tool."""

    target: FoundNode
    upstream: list[ImpactPath]
    downstream: list[ImpactPath]
    tests: list[FoundNode] = Field(default_factory=list)
    docs: list[FoundNode] = Field(default_factory=list)
    layer_breakdown: dict[str, int] = Field(default_factory=dict)


class IngestRepoInput(BaseModel):
    """Input for ingest_repo write tool."""

    repo_url: str
    repo_slug: str
    commit_sha: str | None = Field(
        default=None,
        description="Commit to index; omitted => the resolved tip of `branch`.",
    )
    base_commit_sha: str | None = None
    branch: str = "main"
    force_full: bool = False
    patterns: list[str] = Field(
        default=["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.cs", "**/*.md"],
        description="File glob patterns to ingest.",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        description="Glob patterns of files to leave out (e.g. generated code, vendored copies).",
    )


class IngestRepoOutput(BaseModel):
    """Output for ingest_repo write tool."""

    job_id: str
    status: Literal["completed", "queued", "failed"]
    nodes_added: int
    nodes_modified: int
    nodes_removed: int
    edges_added: int
    edges_removed: int
    duration_seconds: float
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    commit_sha: str | None = Field(
        default=None, description="The commit actually indexed (resolved head)."
    )


class StartIngestOutput(BaseModel):
    """Output for start_ingest_repo."""

    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    already_running: bool = False


class IngestJobSnapshot(BaseModel):
    """Output for get_ingest_job (and GET /api/ingest-jobs/{job_id})."""

    job_id: str
    repo_slug: str
    status: Literal["queued", "running", "completed", "failed"]
    phase: Literal["queued", "cloning", "parsing", "upserting", "cleanup", "mapping_tests", "summarising", "done"]
    files_total: int = 0
    files_done: int = 0
    nodes_added: int = 0
    nodes_modified: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    commit_sha: str | None = None
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0.0


class ChangedFile(BaseModel):
    """One changed file reported by check_repo_changes."""

    path: str
    status: Literal["added", "modified", "deleted", "renamed"]


class CheckRepoChangesInput(BaseModel):
    """Input for check_repo_changes."""

    repo_url: str
    repo_slug: str
    branch: str = "main"
    patterns: list[str] = Field(
        default=["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.cs", "**/*.md"],
        description="File glob patterns considered relevant.",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="Glob patterns of files to leave out."
    )


class CheckRepoChangesOutput(BaseModel):
    """Output for check_repo_changes."""

    indexed_sha: str | None = None
    head_sha: str
    up_to_date: bool
    requires_full: bool
    added: int = 0
    modified: int = 0
    deleted: int = 0
    renamed: int = 0
    files: list[ChangedFile] = Field(default_factory=list)
    truncated: bool = False
    new_commits: int = 0


class ReindexFileInput(BaseModel):
    """Input for reindex_file write tool."""

    repo: str
    file_path: str
    commit_sha: str | None = None


class RefreshEnrichmentInput(BaseModel):
    """Input for refresh_summaries / refresh_embeddings write tool."""

    repo: str | None = None
    node_ids: list[str] | None = None
    only_if_missing: bool = True
    limit: int = 500


class DeleteRepoInput(BaseModel):
    """Input for delete_repo write tool."""

    repo_slug: str
    confirm: bool = False  # must be True to execute; prevents accidental deletes


class RepoStateOutput(BaseModel):
    """State of a repo — returned by the /api/repo-state REST endpoint."""

    repo_slug: str
    last_commit_sha: str | None = None
    last_ingest_at: str | None = None
    node_count: int = 0
    exists: bool = False


# === New Read Tool I/O Models ===


class ProjectSummary(BaseModel):
    """Summary of a single ingested repository."""

    slug: str
    last_commit_sha: str | None = None
    last_ingest_at: str | None = None
    node_count: int
    edge_count: int


class ListProjectsOutput(BaseModel):
    """Output for list_projects tool."""

    projects: list[ProjectSummary]
    total: int


class GetArchitectureInput(BaseModel):
    """Input for get_architecture tool."""

    repo: str


class HotspotNode(BaseModel):
    """A high-degree node in the call graph."""

    id: str
    name: str
    type: str
    file_path: str | None = None
    in_degree: int
    out_degree: int
    total_degree: int


class LayerFlow(BaseModel):
    """Cross-layer call flow."""

    from_layer: str
    to_layer: str
    edge_count: int


class ArchitectureOutput(BaseModel):
    """Output for get_architecture tool."""

    repo: str
    layers: list[dict]
    entry_points: list[dict]
    hotspots: list[HotspotNode]
    layer_flows: list[LayerFlow]


class FindDeadCodeInput(BaseModel):
    """Input for find_dead_code tool."""

    repo: str
    entry_layer: str = "controller"
    limit: int = 50


class DeadCodeOutput(BaseModel):
    """Output for find_dead_code tool."""

    repo: str
    entry_points_analyzed: int
    total_functions: int
    reachable_count: int
    unreachable_count: int
    unreachable_nodes: list[dict]
