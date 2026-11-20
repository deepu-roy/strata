"""Background summary job: progress, usage note, limit and fail-before-spend."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from strata.config import Settings
from strata.ingestion.jobs import REGISTRY
from strata.mcp.tools import write as write_mod

pytestmark = pytest.mark.asyncio


class FakeSummaries:
    def __init__(self, fail_check=False):
        self.fail_check = fail_check
        self.usage = {"calls": 3, "prompt_tokens": 300, "completion_tokens": 30}

    async def check(self):
        if self.fail_check:
            raise RuntimeError("AuthenticationError: bad key")


def _settings():
    s = MagicMock(spec=Settings)
    s.summary = MagicMock(model="anthropic/claude-haiku-5-5", max_concurrent=4)
    s.embedding = MagicMock()
    return s


async def _run(monkeypatch, summaries, nodes, enrich):
    monkeypatch.setattr("strata.providers.summary.base.build_summary_provider", lambda _s: summaries)
    monkeypatch.setattr("strata.providers.embedding.base.build_embedding_provider", lambda _s: object())
    monkeypatch.setattr("strata.ingestion.enrichment.load_unenriched_nodes", AsyncMock(return_value=nodes))
    monkeypatch.setattr("strata.ingestion.enrichment.enrich_batch", enrich)
    out = await write_mod.start_refresh_summaries(MagicMock(), _settings(), "r-%d" % id(nodes), 10)
    while write_mod.get_ingest_job(out.job_id).status in ("queued", "running"):
        await asyncio.sleep(0.01)
    return write_mod.get_ingest_job(out.job_id)


async def test_job_reports_progress_total_and_token_usage(monkeypatch):
    async def enrich(nodes, *, progress=None, **kw):
        for _ in nodes:
            progress.file_done(1)
        return len(nodes), len(nodes)

    snap = await _run(monkeypatch, FakeSummaries(), ["a", "b", "c"], enrich)

    assert (snap.status, snap.files_total, snap.files_done) == ("completed", 3, 3)
    assert any("300 prompt + 30 completion" in n for n in snap.notes)
    assert snap.nodes_modified == 3


async def test_a_model_that_does_not_answer_fails_the_job_before_any_node(monkeypatch):
    enrich = AsyncMock()

    snap = await _run(monkeypatch, FakeSummaries(fail_check=True), ["x"], enrich)

    assert snap.status == "failed" and "bad key" in snap.errors[0]
    enrich.assert_not_called()


async def test_count_pending_summaries():
    client = MagicMock()
    client.execute_query = AsyncMock(return_value=[{"pending": 42}])

    assert await write_mod.count_pending_summaries(client, "logto") == {"repo": "logto", "pending": 42}
