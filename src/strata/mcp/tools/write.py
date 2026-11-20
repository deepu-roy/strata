"""MCP write tools — ingest, refresh, reindex, delete.

All write tools are idempotent where noted (MERGE-based upserts).
They use the same parse → normalize → upsert pipeline as the CLI.
"""

import asyncio
import logging
import uuid
from pathlib import Path

from strata.config import Settings
from strata.domain.models import (
    DeleteRepoInput,
    IngestJobSnapshot,
    IngestRepoInput,
    IngestRepoOutput,
    RefreshEnrichmentInput,
    ReindexFileInput,
    RepoStateOutput,
    StartIngestOutput,
)
from strata.graph.client import Neo4jClient
from strata.ingestion.cleanup import get_repo_meta, hard_delete_repo
from strata.ingestion.git import (
    GitError,
    get_diff,
    get_head_sha,
    is_ancestor,
    redact,
    sync_repo,
)
from strata.ingestion.jobs import REGISTRY, IngestJob, IngestProgress
from strata.ingestion.pipeline import (
    EXCLUDE_DIRS,
    extract_files,
    pat_to_extensions,
    run_pipeline,
    to_ingest_output,
)
from strata.ingestion.upsert import soft_delete_file_nodes, upsert_renamed_from_edge

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _job_id() -> str:
    return uuid.uuid4().hex[:12]


def _is_local_path(repo_url: str) -> bool:
    """Return True if repo_url looks like a local filesystem path."""
    return repo_url.startswith(("/", "./", "../", "file://")) or (
        len(repo_url) > 1 and repo_url[1] == ":"  # Windows C:\...
    )


# ── ingest_repo ───────────────────────────────────────────────────────────────

# Strong refs so fire-and-forget ingest tasks are not garbage collected.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _failed(job_id: str, error: str, **kw) -> IngestRepoOutput:
    return IngestRepoOutput(
        job_id=job_id, status="failed",
        nodes_added=0, nodes_modified=0, nodes_removed=0,
        edges_added=0, edges_removed=0, duration_seconds=0.0,
        errors=[error], **kw,
    )


async def _run_ingest(
    client: Neo4jClient,
    settings: Settings,
    inp: IngestRepoInput,
    job_id: str,
    progress: IngestProgress,
) -> IngestRepoOutput:
    """The single ingest implementation shared by blocking and async paths.

    Workflow:
    1. Local path -> ingest in place. Remote URL -> ``sync_repo`` (clone/fetch,
       move the working tree to ``commit_sha`` or the branch tip, verify HEAD).
    2. With ``base_commit_sha`` (and not ``force_full``) only the diff is
       processed; an unreachable base (force-push) falls back to a full ingest
       and adds a note.
    3. parse -> normalize -> upsert -> cleanup; the :Repo node records the
       ACTUAL head sha.
    """
    notes: list[str] = []
    logger.info(
        f"[{job_id}] ingest_repo start: repo={inp.repo_slug} "
        f"commit={(inp.commit_sha or 'branch:' + inp.branch)[:40]} "
        f"force_full={inp.force_full}"
    )

    def _with_meta(out: IngestRepoOutput, sha: str | None) -> IngestRepoOutput:
        out.notes = notes + [n for n in out.notes if n not in notes]
        out.commit_sha = sha
        return out

    try:
        # ── Resolve repo path + head sha ──────────────────────────────────────
        head_sha: str | None = inp.commit_sha
        if _is_local_path(inp.repo_url):
            repo_path = Path(inp.repo_url.replace("file://", ""))
            if not repo_path.is_dir():
                return _failed(job_id, f"Local path does not exist: {inp.repo_url}")
            if head_sha is None:
                try:
                    head_sha = get_head_sha(repo_path)
                except GitError:
                    head_sha = None  # plain directory, not a git repo
        else:
            progress.set_phase("cloning")
            repo_path, head_sha = await asyncio.to_thread(
                sync_repo,
                inp.repo_url,
                Path(settings.workdir),
                branch=inp.branch,
                commit_sha=inp.commit_sha,
            )
        progress.set_commit(head_sha)

        # ── Determine file list ───────────────────────────────────────────────
        explicit_files: list[str] | None = None

        if not inp.force_full and inp.base_commit_sha and head_sha:
            if not await asyncio.to_thread(
                is_ancestor, repo_path, inp.base_commit_sha, head_sha
            ):
                msg = (
                    f"base_commit_sha {inp.base_commit_sha[:12]} is not reachable from "
                    f"{head_sha[:12]} (force-push or unknown commit); fell back to a full ingest"
                )
                logger.warning(f"[{job_id}] {msg}")
                notes.append(msg)
            else:
                try:
                    diff = get_diff(repo_path, inp.base_commit_sha, head_sha)
                    explicit_files = [
                        f for f in diff.changed
                        if any(f.endswith(ext) for pat in inp.patterns
                               for ext in pat_to_extensions(pat))
                        and not any(part in EXCLUDE_DIRS for part in Path(f).parts)
                    ]
                    logger.info(
                        f"[{job_id}] Incremental: {len(explicit_files)} changed files "
                        f"(added={len(diff.added)}, modified={len(diff.modified)}, "
                        f"deleted={len(diff.deleted)}, renamed={len(diff.renamed)})"
                    )

                    # ── Handle deleted files — soft-delete their nodes ────────
                    if diff.deleted:
                        for deleted_path in diff.deleted:
                            await soft_delete_file_nodes(client, inp.repo_slug, deleted_path)

                    # ── Handle renamed files — soft-delete old, emit RENAMED_FROM
                    for old_path, new_path in diff.renamed:
                        await soft_delete_file_nodes(client, inp.repo_slug, old_path)
                        ok = await upsert_renamed_from_edge(
                            client, inp.repo_slug, old_path, new_path, head_sha
                        )
                        if ok:
                            logger.info(f"[{job_id}] Rename edge: {old_path} → {new_path}")

                    if not explicit_files and not diff.deleted and not diff.renamed:
                        logger.info(f"[{job_id}] No relevant file changes — skipping ingest")
                        return _with_meta(IngestRepoOutput(
                            job_id=job_id, status="completed",
                            nodes_added=0, nodes_modified=0, nodes_removed=0,
                            edges_added=0, edges_removed=0, duration_seconds=0.0,
                        ), head_sha)
                except Exception as e:
                    logger.warning(f"[{job_id}] Diff failed, falling back to full ingest: {e}")
                    notes.append(f"diff failed ({redact(str(e))[:200]}); fell back to a full ingest")
                    explicit_files = None

        # ── Run pipeline ──────────────────────────────────────────────────────
        changed_files = set(explicit_files) if explicit_files else None
        result = await run_pipeline(
            client=client,
            repo_path=repo_path,
            repo_slug=inp.repo_slug,
            patterns=inp.patterns,
            commit_sha=head_sha,
            explicit_files=explicit_files,
            changed_files=changed_files,
            do_soft_delete=True,
            progress=progress,
        )

        output = _with_meta(to_ingest_output(job_id, result), head_sha)
        logger.info(
            f"[{job_id}] ingest_repo done: status={output.status} "
            f"nodes={output.nodes_added} edges={output.edges_added} "
            f"removed={output.nodes_removed} duration={output.duration_seconds}s"
        )
        return output

    except Exception as e:
        err = redact(str(e))
        logger.error(f"[{job_id}] ingest_repo failed: {err}", exc_info=True)
        return _with_meta(_failed(job_id, err), None)


async def _execute_job(
    job: IngestJob,
    client: Neo4jClient,
    settings: Settings,
    inp: IngestRepoInput,
) -> IngestRepoOutput:
    """Run the core under *job*, always leaving it in a terminal state."""
    job.status = "running"
    try:
        out = await _run_ingest(client, settings, inp, job.job_id, job)
    except BaseException as e:  # incl. cancellation: never leave a job "running"
        job.finish("failed", errors=[redact(f"{type(e).__name__}: {e}")])
        raise
    job.nodes_added = out.nodes_added
    job.nodes_modified = out.nodes_modified
    job.nodes_removed = out.nodes_removed
    job.edges_added = out.edges_added
    job.edges_removed = out.edges_removed
    if out.commit_sha:
        job.commit_sha = out.commit_sha
    for n in out.notes:
        job.note(n)
    if out.status == "completed":
        job.file_done(job.files_total)
    job.finish("failed" if out.status == "failed" else "completed", errors=out.errors)
    return out


async def ingest_repo(
    client: Neo4jClient,
    settings: Settings,
    inp: IngestRepoInput,
) -> IngestRepoOutput:
    """Blocking ingest — runs to completion and returns the stats.

    Remote URLs are cloned/fetched and checked out at ``commit_sha`` (or the tip
    of ``branch``).  The run is registered as a job (``job_id`` in the output)
    so ``get_ingest_job`` can report on it.  If a job for the same repo is
    already active, nothing new is started: the existing job id is returned
    with status ``queued`` and a note.
    """
    job, created = REGISTRY.start(inp.repo_slug)
    if not created:
        return IngestRepoOutput(
            job_id=job.job_id, status="queued",
            nodes_added=0, nodes_modified=0, nodes_removed=0,
            edges_added=0, edges_removed=0, duration_seconds=0.0,
            notes=[(
                f"an ingest for '{inp.repo_slug}' is already running; "
                "poll get_ingest_job with this job_id"
            )],
        )
    return await _execute_job(job, client, settings, inp)


async def start_ingest_repo(
    client: Neo4jClient,
    settings: Settings,
    inp: IngestRepoInput,
) -> StartIngestOutput:
    """Start an ingest in the background and return immediately.

    One active job per repo_slug: when one exists its id is returned with
    ``already_running=True``.  Failures are captured into the job.
    """
    job, created = REGISTRY.start(inp.repo_slug)
    if not created:
        return StartIngestOutput(
            job_id=job.job_id, status=job.status, already_running=True  # type: ignore[arg-type]
        )

    async def _bg() -> None:
        try:
            await _execute_job(job, client, settings, inp)
        except BaseException as e:  # job already marked failed in _execute_job
            logger.error(f"[{job.job_id}] background ingest crashed: {redact(str(e))}")
            if not isinstance(e, Exception):
                raise

    task = asyncio.create_task(_bg())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return StartIngestOutput(job_id=job.job_id, status="queued", already_running=False)


def get_ingest_job(job_id: str) -> IngestJobSnapshot | None:
    """Return a snapshot of an ingest job, or None if unknown/expired."""
    job = REGISTRY.get(job_id)
    return job.snapshot() if job else None


# pat_to_extensions is imported as pat_to_extensions at module level above


# ── reindex_file ──────────────────────────────────────────────────────────────


async def reindex_file(
    client: Neo4jClient,
    inp: ReindexFileInput,
) -> dict:
    """Re-extract and upsert a single file.

    Useful during development to update a file without re-running a full ingest.

    Args:
        client: Connected Neo4j client.
        inp: Reindex parameters.

    Returns:
        Dict with ``status``, ``nodes_upserted``, ``edges_upserted``.
    """
    # Locate the file — check a few candidate root layouts
    candidates = [
        Path(inp.file_path),
        Path(".") / inp.repo / inp.file_path,
        Path(".") / inp.file_path,
    ]
    repo_path: Path | None = None
    rel_path = inp.file_path

    for cand in candidates:
        if cand.is_file():
            repo_path = cand.parent
            rel_path = cand.name
            break

    if repo_path is None:
        return {"status": "failed", "error": f"File not found: {inp.file_path}"}

    try:
        nodes, edges = await extract_files(repo_path, [rel_path])
        from strata.ingestion.upsert import normalize_and_upsert
        n, e = await normalize_and_upsert(client, nodes, edges, inp.repo)
        return {"status": "completed", "nodes_upserted": n, "edges_upserted": e}
    except Exception as ex:
        logger.error(f"reindex_file failed for {inp.file_path}: {ex}")
        return {"status": "failed", "error": str(ex)}


# ── refresh_summaries ─────────────────────────────────────────────────────────


async def refresh_summaries(
    client: Neo4jClient,
    settings: Settings,
    inp: RefreshEnrichmentInput,
) -> dict:
    """Re-run summary + embedding enrichment for nodes that need it.

    Args:
        client: Connected Neo4j client.
        settings: Application settings (provider config).
        inp: Which nodes to re-enrich and whether to skip already-enriched ones.

    Returns:
        Dict with ``enriched``, ``written``.
    """
    from strata.ingestion.enrichment import enrich_batch, load_unenriched_nodes
    from strata.providers.embedding.base import build_embedding_provider
    from strata.providers.summary.base import build_summary_provider

    try:
        summary_provider = build_summary_provider(settings.summary)
        embedding_provider = build_embedding_provider(settings.embedding)
    except Exception as e:
        return {"status": "failed", "error": f"Provider init failed: {e}"}

    if inp.node_ids:
        # Load specific nodes by ID — use load_unenriched_nodes with a manual query
        # to get NormalizedNode objects consistent with enrich_batch expectations
        q_rows = await client.execute_query(
            "MATCH (n:CodeNode) WHERE n.id IN $ids RETURN n",
            {"ids": inp.node_ids},
        )
        rows: list = [r["n"] if isinstance(r, dict) else r for r in q_rows]
    else:
        rows = await load_unenriched_nodes(client, repo=inp.repo, limit=inp.limit)

    if inp.only_if_missing:
        # NormalizedNode objects use attribute access; raw dicts use .get()
        def _has_summary(r) -> bool:
            return bool(r.summary if hasattr(r, "summary") else r.get("summary"))
        rows = [r for r in rows if not _has_summary(r)]

    if not rows:
        return {"status": "completed", "enriched": 0, "written": 0}

    enriched, written = await enrich_batch(
        rows,
        summary_provider=summary_provider,
        embedding_provider=embedding_provider,
        client=client,
        max_concurrent=settings.summary.max_concurrent,
    )
    return {"status": "completed", "enriched": enriched, "written": written}


# ── start_refresh_summaries / count_pending_summaries ────────────────────────


async def count_pending_summaries(client: Neo4jClient, repo: str) -> dict:
    """How many nodes of ``repo`` have no summary yet (what an enrich run would work on)."""
    rows = await client.execute_query(
        "MATCH (n) WHERE n.repo = $repo AND n.summary IS NULL "
        "AND labels(n)[0] IN ['Class', 'Function', 'Method', 'Interface', 'File', 'Enum'] "
        "RETURN count(n) AS pending",
        {"repo": repo},
    )
    return {"repo": repo, "pending": int(rows[0]["pending"]) if rows else 0}


async def start_refresh_summaries(
    client: Neo4jClient, settings: Settings, repo: str, limit: int
) -> StartIngestOutput:
    """Summarise up to ``limit`` unsummarised nodes in the background; poll ``get_ingest_job``.

    One active job per repo. The job reports nodes done of nodes total, and on completion a
    note with the model calls and tokens used. A model that does not answer fails the job
    before any node is processed.
    """
    job, created = REGISTRY.start(f"{repo}#summaries")
    if not created:
        return StartIngestOutput(
            job_id=job.job_id, status=job.status, already_running=True  # type: ignore[arg-type]
        )

    async def _bg() -> None:
        from strata.ingestion.enrichment import enrich_batch, load_unenriched_nodes
        from strata.providers.embedding.base import build_embedding_provider
        from strata.providers.summary.base import build_summary_provider

        job.status = "running"
        try:
            job.set_phase("summarising")
            summaries = build_summary_provider(settings.summary)
            embeddings = build_embedding_provider(settings.embedding)
            check = getattr(summaries, "check", None)
            if check is not None:
                await check()  # a bad key fails here, before any node is sent
            nodes = await load_unenriched_nodes(client, repo=repo, limit=limit)
            job.set_total(len(nodes))
            enriched = written = 0
            if nodes:
                enriched, written = await enrich_batch(
                    nodes, summary_provider=summaries, embedding_provider=embeddings,
                    client=client, max_concurrent=settings.summary.max_concurrent, progress=job,
                )
            job.nodes_modified = written
            usage = getattr(summaries, "usage", None)
            job.note(f"model: {settings.summary.model}")
            job.note(f"{enriched} summarised, {written} written")
            if usage:
                job.note(
                    f"{usage['calls']} model calls, {usage['prompt_tokens']} prompt + "
                    f"{usage['completion_tokens']} completion tokens"
                )
            job.finish("completed")
        except BaseException as e:  # never leave a job "running"
            job.finish("failed", errors=[redact(f"{type(e).__name__}: {e}")])
            if not isinstance(e, Exception):
                raise

    task = asyncio.create_task(_bg())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return StartIngestOutput(job_id=job.job_id, status="queued", already_running=False)


# ── delete_repo ───────────────────────────────────────────────────────────────


async def delete_repo(
    client: Neo4jClient,
    inp: DeleteRepoInput,
) -> dict:
    """Hard-delete all nodes and edges for a repository.

    Requires ``confirm=True`` to prevent accidental deletions.

    Args:
        client: Connected Neo4j client.
        inp: Delete parameters.

    Returns:
        Dict with ``status``, ``nodes_deleted``.
    """
    if not inp.confirm:
        return {
            "status": "aborted",
            "reason": "confirm=False — pass confirm=true to execute the deletion",
        }

    try:
        cnt = await hard_delete_repo(client, inp.repo_slug)
        return {"status": "completed", "nodes_deleted": cnt}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── repo_state ────────────────────────────────────────────────────────────────


async def get_repo_state(
    client: Neo4jClient,
    repo_slug: str,
) -> RepoStateOutput:
    """Return the current ingestion state of a repository.

    Used by the GitHub Actions workflow to determine the base commit
    for incremental ingestion.

    Args:
        client: Connected Neo4j client.
        repo_slug: Repository slug.

    Returns:
        RepoStateOutput with last commit SHA, node count, etc.
    """
    meta = await get_repo_meta(client, repo_slug)
    if not meta:
        return RepoStateOutput(repo_slug=repo_slug, exists=False)

    # Count nodes
    rows = await client.execute_query(
        "MATCH (n:CodeNode {repo: $repo}) RETURN count(n) AS cnt",
        {"repo": repo_slug},
    )
    cnt = rows[0]["cnt"] if rows else 0

    last_ingest = meta.get("last_ingest_at")
    return RepoStateOutput(
        repo_slug=repo_slug,
        last_commit_sha=meta.get("last_commit_sha"),
        last_ingest_at=str(last_ingest) if last_ingest else None,
        node_count=cnt,
        exists=True,
    )


# ── run_test_map ──────────────────────────────────────────────────────────────


async def run_test_map(
    client: Neo4jClient,
    repo_slug: str,
    min_weight: float = 0.5,
) -> dict:
    """Infer and emit TESTS edges for a repository.

    Runs all three strategies (CALLS graph, file-name, path mirror) and
    writes TESTS edges for matches above *min_weight*.

    Args:
        client: Connected Neo4j client.
        repo_slug: Repository slug.
        min_weight: Minimum combined confidence to emit an edge.

    Returns:
        Dict with ``status``, ``edges_written``, ``edges_skipped``.
    """
    from strata.ingestion.tests_mapper import map_tests
    try:
        result = await map_tests(client, repo_slug, min_weight=min_weight)
        return {
            "status": "completed" if not result.errors else "partial",
            "edges_written": result.edges_written,
            "edges_skipped": result.edges_skipped,
            "errors": result.errors,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── refresh_doc_links ─────────────────────────────────────────────────────────


async def refresh_doc_links(
    client: Neo4jClient,
    settings: Settings,
    repo_slug: str,
    use_embeddings: bool = True,
    batch_size: int = 20,
    min_confidence: float = 0.3,
) -> dict:
    """Infer DOCUMENTS edges from doc sections to code nodes using the LLM.

    For each :Section node, discovers candidate code nodes (name match +
    optional vector lookup) and calls the LLM to classify which are
    actually documented.  Emits DOCUMENTS edges with the confidence weight.

    Args:
        client: Connected Neo4j client.
        settings: App settings (LLM + embedding provider config).
        repo_slug: Repository slug.
        use_embeddings: Use vector neighbour lookup in addition to name matching.
        batch_size: Sections per LLM batch.
        min_confidence: Minimum LLM confidence to emit a DOCUMENTS edge.

    Returns:
        Dict with ``status``, ``sections_processed``, ``edges_written``.
    """
    from strata.ingestion.docs_linker import link_docs
    try:
        result = await link_docs(
            client, settings, repo_slug,
            batch_size=batch_size,
            use_embeddings=use_embeddings,
            min_confidence=min_confidence,
        )
        return {
            "status": "completed" if not result.errors else "partial",
            "sections_processed": result.sections_processed,
            "edges_written": result.edges_written,
            "edges_skipped": result.edges_skipped,
            "errors": result.errors[:10],  # cap for MCP response size
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}
