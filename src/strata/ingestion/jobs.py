"""In-process registry of ingest jobs with progress reporting.

Jobs live in memory only (TTL ~1h, capped at ~100 finished jobs) and there is at
most one *active* (queued/running) job per ``repo_slug``.  All registry
operations are synchronous and lock-protected, so they are safe from asyncio
tasks and from worker threads.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from strata.domain.models import IngestJobSnapshot

JOB_TTL_SECONDS = 3600.0
MAX_JOBS = 100

ACTIVE_STATUSES = ("queued", "running")


class IngestProgress:
    """No-op progress sink; the ingest core calls these hooks."""

    def set_phase(self, phase: str) -> None:  # pragma: no cover - trivial
        pass

    def set_total(self, total: int) -> None:  # pragma: no cover - trivial
        pass

    def file_done(self, n: int = 1) -> None:  # pragma: no cover - trivial
        pass

    def set_commit(self, sha: str | None) -> None:  # pragma: no cover - trivial
        pass

    def note(self, text: str) -> None:  # pragma: no cover - trivial
        pass


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class IngestJob(IngestProgress):
    """A single ingest run; doubles as the progress sink for the core."""

    job_id: str
    repo_slug: str
    status: str = "queued"
    phase: str = "queued"
    files_total: int = 0
    files_done: int = 0
    nodes_added: int = 0
    nodes_modified: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    commit_sha: str | None = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # progress hooks
    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def set_total(self, total: int) -> None:
        self.files_total = total

    def file_done(self, n: int = 1) -> None:
        self.files_done = min(self.files_done + n, self.files_total or self.files_done + n)

    def set_commit(self, sha: str | None) -> None:
        self.commit_sha = sha

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def finish(self, status: str, *, errors: list[str] | None = None) -> None:
        self.status = status
        self.phase = "done"
        if errors:
            self.errors.extend(e for e in errors if e not in self.errors)
        self.finished_at = time.time()

    def snapshot(self) -> IngestJobSnapshot:
        end = self.finished_at if self.finished_at is not None else time.time()
        return IngestJobSnapshot(
            job_id=self.job_id,
            repo_slug=self.repo_slug,
            status=self.status,  # type: ignore[arg-type]
            phase=self.phase,  # type: ignore[arg-type]
            files_total=self.files_total,
            files_done=self.files_done,
            nodes_added=self.nodes_added,
            nodes_modified=self.nodes_modified,
            nodes_removed=self.nodes_removed,
            edges_added=self.edges_added,
            edges_removed=self.edges_removed,
            commit_sha=self.commit_sha,
            errors=list(self.errors),
            notes=list(self.notes),
            started_at=_iso(self.started_at),
            finished_at=_iso(self.finished_at),
            elapsed_seconds=round(max(0.0, end - self.started_at), 2),
        )


class JobRegistry:
    """Bounded, TTL-expiring store of :class:`IngestJob` objects."""

    def __init__(self, ttl: float = JOB_TTL_SECONDS, max_jobs: int = MAX_JOBS) -> None:
        self.ttl = ttl
        self.max_jobs = max_jobs
        self._jobs: dict[str, IngestJob] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.time()
        for jid in [
            j.job_id for j in self._jobs.values()
            if not j.active and j.finished_at is not None and now - j.finished_at > self.ttl
        ]:
            del self._jobs[jid]
        if len(self._jobs) > self.max_jobs:
            finished = sorted(
                (j for j in self._jobs.values() if not j.active),
                key=lambda j: j.finished_at or 0.0,
            )
            for job in finished:
                if len(self._jobs) <= self.max_jobs:
                    break
                del self._jobs[job.job_id]

    def start(self, repo_slug: str) -> tuple[IngestJob, bool]:
        """Create a job for *repo_slug*, or return the active one.

        Returns:
            ``(job, created)`` — ``created`` is False when an active job already
            exists for the slug (that job is returned).
        """
        with self._lock:
            self._prune()
            for job in self._jobs.values():
                if job.repo_slug == repo_slug and job.active:
                    return job, False
            job = IngestJob(job_id=uuid.uuid4().hex[:12], repo_slug=repo_slug)
            self._jobs[job.job_id] = job
            return job, True

    def get(self, job_id: str) -> IngestJob | None:
        with self._lock:
            self._prune()
            return self._jobs.get(job_id)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


REGISTRY = JobRegistry()
