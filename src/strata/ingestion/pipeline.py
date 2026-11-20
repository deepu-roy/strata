"""Ingestion pipeline — orchestrates parse → normalize → enrich → upsert.

This module extracts the core pipeline logic from the CLI so it can be
called by both the CLI (``strata ingest``) and the MCP write tool
(``ingest_repo``).
"""

import asyncio
import fnmatch
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from strata.domain.models import IngestRepoOutput, RawEdge, RawNode
from strata.graph.client import Neo4jClient
from strata.ingestion.cleanup import soft_delete_orphans, update_repo_meta
from strata.ingestion.sources.code_csharp import CSharpSource
from strata.ingestion.sources.code_go import GoSource
from strata.ingestion.sources.code_java import JavaSource
from strata.ingestion.sources.code_python import PythonSource
from strata.ingestion.sources.code_typescript import TypeScriptSource
from strata.ingestion.sources.docs_markdown import MarkdownSource
from strata.ingestion.upsert import normalize_and_upsert

logger = logging.getLogger(__name__)


# ── Directories always excluded regardless of glob patterns ───────────────────

EXCLUDE_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".svn", "dist", "build", "out",
    "bin", "obj", ".next", ".nuxt", ".angular", "coverage", "__pycache__",
    ".venv", "venv", ".tox", "target", ".cache",
})


@dataclass
class PipelineResult:
    """Detailed outcome of a single pipeline run."""

    nodes_added: int = 0
    nodes_modified: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def pat_to_extensions(pattern: str) -> list[str]:
    """Extract file extensions from a glob pattern like ``**/*.cs``.

    >>> pat_to_extensions("**/*.cs")
    ['.cs']
    >>> pat_to_extensions("src/**")
    []
    """
    suffix = Path(pattern).suffix
    return [suffix] if suffix else []


def is_excluded(path: str, exclude_patterns: list[str] | None) -> bool:
    """Whether ``path`` matches any exclude glob (full path, or base name for ``**/x`` forms)."""
    for pat in exclude_patterns or []:
        simple = pat.lstrip("*").lstrip("/")
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.basename(path), simple):
            return True
    return False


def collect_files(
    repo_path: Path,
    patterns: list[str],
    explicit_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """Return file paths matching *patterns* in *repo_path*, excluding junk dirs.

    Uses os.walk with early directory pruning so excluded dirs (node_modules,
    .angular, dist, …) are never descended into — critical for performance on
    virtiofs/NFS mounts where every directory listing is a round-trip.

    Args:
        repo_path: Repository root.
        patterns: Glob patterns (e.g. ``["**/*.cs", "**/*.ts"]``).
        explicit_files: If provided, use this list directly (already resolved).
        exclude_patterns: Additional glob patterns to exclude (e.g. ``["**/generated/**"]``).

    Returns:
        Sorted, deduplicated list of relative paths.
    """
    if explicit_files is not None:
        return sorted(f for f in set(explicit_files) if not is_excluded(f, exclude_patterns))

    # Extract just the extensions we care about from patterns like "**/*.ts"
    extensions: set[str] = set()
    for pat in patterns:
        suffix = Path(pat).suffix
        if suffix:
            extensions.add(suffix.lower())

    seen: set[str] = set()
    root_str = str(repo_path)

    for dirpath, dirnames, filenames in os.walk(root_str):
        # Prune excluded dirs in-place so os.walk never descends into them
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        rel_dir = os.path.relpath(dirpath, root_str)

        for fname in filenames:
            if not extensions or os.path.splitext(fname)[1].lower() in extensions:
                rel = os.path.join(rel_dir, fname) if rel_dir != "." else fname
                # Normalise path separators on Windows
                rel = rel.replace(os.sep, "/")
                seen.add(rel)

    # Apply user-supplied exclude globs against the collected paths
    seen = {f for f in seen if not is_excluded(f, exclude_patterns)}

    return sorted(seen)


async def _collect_from_source(
    source,
    repo_path: str,
    files: list[str],
    nodes: list,
    edges: list,
    progress=None,
    seen: set | None = None,
) -> None:
    """Drain a source's async iterator into nodes/edges lists.

    Args:
        source: An ingestion source with an ``extract`` async iterator.
        repo_path: Repository root directory.
        files: Relative file paths to extract.
        nodes: Accumulator list for RawNode objects.
        edges: Accumulator list for RawEdge objects.
    """
    async for item in source.extract(repo_path, files):
        if isinstance(item, RawNode):
            nodes.append(item)
            if progress is not None and seen is not None:
                fp = getattr(item, "file_path", None)
                if fp and fp not in seen:
                    seen.add(fp)
                    progress.file_done()
        else:
            edges.append(item)


async def extract_files(
    repo_path: Path,
    files: list[str],
    progress=None,
) -> tuple[list[RawNode], list[RawEdge]]:
    """Run tree-sitter extraction on all *files*, routing by extension.

    Code sources (TypeScript, C#, Java) are run concurrently via
    ``asyncio.gather``.  Markdown must run afterwards because it needs the
    symbol names produced by the code sources.

    Args:
        repo_path: Repository root.
        files: Relative file paths to extract.
        progress: Optional progress sink; ``file_done()`` fires once per file
            as its first node is produced.

    Returns:
        Tuple of (all_nodes, all_edges).
    """
    all_nodes: list[RawNode] = []
    all_edges: list[RawEdge] = []
    seen: set[str] = set()

    ts_files = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    cs_files = [f for f in files if f.endswith(".cs")]
    java_files = [f for f in files if f.endswith(".java")]
    py_files = [f for f in files if f.endswith(".py")]
    go_files = [f for f in files if f.endswith(".go")]
    md_files = [f for f in files if f.endswith((".md", ".markdown"))]

    # Run all code sources concurrently
    tasks = []
    if ts_files:
        tasks.append(_collect_from_source(TypeScriptSource(), str(repo_path), ts_files, all_nodes, all_edges, progress, seen))
    if cs_files:
        tasks.append(_collect_from_source(CSharpSource(), str(repo_path), cs_files, all_nodes, all_edges, progress, seen))
    if java_files:
        tasks.append(_collect_from_source(JavaSource(), str(repo_path), java_files, all_nodes, all_edges, progress, seen))

    if py_files:
        tasks.append(_collect_from_source(PythonSource(), str(repo_path), py_files, all_nodes, all_edges, progress, seen))
    if go_files:
        tasks.append(_collect_from_source(GoSource(), str(repo_path), go_files, all_nodes, all_edges, progress, seen))

    if tasks:
        await asyncio.gather(*tasks)

    # Markdown MUST run after code sources (needs symbol names)
    if md_files:
        symbol_names = {n.name for n in all_nodes if n.type in ("class", "function", "interface")}
        md_source = MarkdownSource()
        async for item in md_source.extract(str(repo_path), md_files, symbol_names=symbol_names):
            if isinstance(item, RawNode):
                all_nodes.append(item)
                fp = getattr(item, "file_path", None)
                if progress is not None and fp and fp not in seen:
                    seen.add(fp)
                    progress.file_done()
            else:
                all_edges.append(item)

    return all_nodes, all_edges


async def run_pipeline(
    client: Neo4jClient,
    repo_path: Path,
    repo_slug: str,
    patterns: list[str],
    commit_sha: str | None = None,
    explicit_files: list[str] | None = None,
    changed_files: set[str] | None = None,
    do_soft_delete: bool = True,
    exclude_patterns: list[str] | None = None,
    progress=None,
) -> PipelineResult:
    """Run the full parse → normalize → upsert → cleanup pipeline.

    Args:
        client: Connected Neo4j client.
        repo_path: Absolute path to the repository root.
        repo_slug: Repository slug (used as the node namespace).
        patterns: Glob patterns for file discovery.
        commit_sha: Current commit SHA (stored on nodes for soft-delete tracking).
        explicit_files: If set, skip file discovery and use this list.
        do_soft_delete: If True, soft-delete orphaned nodes after upsert.
        progress: Optional progress sink (see ``strata.ingestion.jobs``).

    Returns:
        PipelineResult with counters and any non-fatal errors.
    """
    result = PipelineResult()
    t0 = time.monotonic()

    try:
        if progress is not None:
            progress.set_phase("parsing")
        files = collect_files(repo_path, patterns, explicit_files, exclude_patterns)
        if progress is not None:
            progress.set_total(len(files))
        if not files:
            logger.warning(f"No files found for repo={repo_slug}")
            result.duration_seconds = time.monotonic() - t0
            return result

        logger.info(f"Extracting {len(files)} files for repo={repo_slug}")
        all_nodes, all_edges = await extract_files(repo_path, files, progress)
        logger.info(f"Extracted {len(all_nodes)} nodes, {len(all_edges)} edges")
        if progress is not None:
            progress.file_done(len(files))  # files that yielded no nodes
            progress.set_phase("upserting")

        nodes_upserted, edges_upserted = await normalize_and_upsert(
            client, all_nodes, all_edges, repo_slug,
            changed_files=changed_files,
            seen_commit=commit_sha,
            repo_path=repo_path,
            replace_edges=explicit_files is None or bool(changed_files),
        )
        result.nodes_added = nodes_upserted
        result.edges_added = edges_upserted

        if progress is not None:
            progress.set_phase("cleanup")
        # Always persist source_path so enrichment can read files without
        # the caller supplying the path again.
        await update_repo_meta(
            client, repo_slug,
            commit_sha=commit_sha or "HEAD",
            source_path=str(repo_path.resolve()),
        )
        if commit_sha:
            # ``seen_commit`` above stamped exactly the nodes found in this run; stamping whole
            # files by path would also stamp symbols that no longer exist in them.
            if do_soft_delete:
                removed = await soft_delete_orphans(
                    client,
                    repo_slug,
                    commit_sha,
                    # A partial (incremental) run must not sweep the files it skipped.
                    file_paths=files if explicit_files is not None else None,
                )
                result.nodes_removed = removed

        await _map_tests(client, repo_slug, progress)

    except Exception as e:
        logger.error(f"Pipeline error for repo={repo_slug}: {e}", exc_info=True)
        result.errors.append(str(e))

    result.duration_seconds = round(time.monotonic() - t0, 2)
    return result


async def _map_tests(client: Neo4jClient, repo_slug: str, progress) -> None:
    """Infer TESTS edges once the code graph is complete, so every consumer gets them.

    Best effort: a mapping failure is logged and leaves the index itself intact (``strata
    test-map`` can be re-run on its own).
    """
    from strata.ingestion.tests_mapper import map_tests

    if progress is not None:
        progress.set_phase("mapping_tests")
    try:
        mapped = await map_tests(client, repo_slug)
        logger.info(f"Mapped {mapped.edges_written} TESTS edges for repo={repo_slug}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"test mapping skipped for repo={repo_slug}: {e}")


def to_ingest_output(
    job_id: str,
    result: PipelineResult,
) -> IngestRepoOutput:
    """Convert a PipelineResult to the MCP IngestRepoOutput model.

    Args:
        job_id: Unique identifier for this ingestion job.
        result: Raw pipeline result.

    Returns:
        Structured output ready for MCP serialization.
    """
    status = "failed" if result.errors else "completed"
    return IngestRepoOutput(
        job_id=job_id,
        status=status,
        nodes_added=result.nodes_added,
        nodes_modified=result.nodes_modified,
        nodes_removed=result.nodes_removed,
        edges_added=result.edges_added,
        edges_removed=result.edges_removed,
        duration_seconds=result.duration_seconds,
        errors=result.errors,
    )
