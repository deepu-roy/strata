"""Read-only change detection: what changed upstream since the last ingest?"""

import asyncio
import logging
from pathlib import Path

from code_kg.config import Settings
from code_kg.domain.models import ChangedFile, CheckRepoChangesInput, CheckRepoChangesOutput
from code_kg.graph.client import Neo4jClient
from code_kg.ingestion.cleanup import get_repo_meta
from code_kg.ingestion.git import (
    count_commits,
    get_diff,
    get_head_sha,
    is_ancestor,
    sync_repo,
)
from code_kg.ingestion.pipeline import EXCLUDE_DIRS, pat_to_extensions
from code_kg.mcp.tools.write import _is_local_path

logger = logging.getLogger(__name__)

MAX_FILES = 200


def _relevant(path: str, exts: set[str]) -> bool:
    return path.endswith(tuple(exts)) and not any(
        part in EXCLUDE_DIRS for part in Path(path).parts
    )


async def check_repo_changes(
    client: Neo4jClient,
    settings: Settings,
    inp: CheckRepoChangesInput,
) -> CheckRepoChangesOutput:
    """Compare the remote branch tip with the last indexed commit.

    Fetches the clone (no checkout, no re-indexing) and diffs
    ``indexed..head`` with the same pattern/exclude-dir filter ingest uses.
    A never-indexed repo, or an indexed sha that is not an ancestor of head
    (force-push / unknown commit), reports ``requires_full=True``.
    """
    if _is_local_path(inp.repo_url):
        repo_path = Path(inp.repo_url.replace("file://", ""))
        head = get_head_sha(repo_path)
    else:
        repo_path, head = await asyncio.to_thread(
            sync_repo, inp.repo_url, Path(settings.workdir),
            branch=inp.branch, checkout=False,
        )

    meta = await get_repo_meta(client, inp.repo_slug)
    indexed = (meta or {}).get("last_commit_sha")
    if indexed == "HEAD":  # stored when the index had no real commit
        indexed = None

    if not indexed:
        return CheckRepoChangesOutput(
            indexed_sha=None, head_sha=head, up_to_date=False, requires_full=True
        )
    if indexed == head:
        return CheckRepoChangesOutput(
            indexed_sha=indexed, head_sha=head, up_to_date=True, requires_full=False
        )
    if not await asyncio.to_thread(is_ancestor, repo_path, indexed, head):
        return CheckRepoChangesOutput(
            indexed_sha=indexed, head_sha=head, up_to_date=False, requires_full=True
        )

    exts = {e for pat in inp.patterns for e in pat_to_extensions(pat)}
    diff = await asyncio.to_thread(get_diff, repo_path, indexed, head)
    added = [f for f in diff.added if _relevant(f, exts)]
    modified = [f for f in diff.modified if _relevant(f, exts)]
    deleted = [f for f in diff.deleted if _relevant(f, exts)]
    renamed = [(o, n) for o, n in diff.renamed if _relevant(n, exts)]

    files = (
        [ChangedFile(path=p, status="added") for p in added]
        + [ChangedFile(path=p, status="modified") for p in modified]
        + [ChangedFile(path=p, status="deleted") for p in deleted]
        + [ChangedFile(path=n, status="renamed") for _, n in renamed]
    )
    commits = await asyncio.to_thread(count_commits, repo_path, indexed, head)
    return CheckRepoChangesOutput(
        indexed_sha=indexed, head_sha=head, up_to_date=False, requires_full=False,
        added=len(added), modified=len(modified), deleted=len(deleted),
        renamed=len(renamed), files=files[:MAX_FILES],
        truncated=len(files) > MAX_FILES, new_commits=commits,
    )
