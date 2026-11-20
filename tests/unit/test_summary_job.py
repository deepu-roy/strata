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
    monkeypatch.setattr("strata.ingestion.enrichment.load_priority_nodes", AsyncMock(return_value=nodes))
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


async def test_priority_loader_passes_file_heat_and_builds_typed_nodes():
    from strata.ingestion.enrichment import load_priority_nodes

    calls = []

    async def run(query, params=None):
        calls.append(params)
        if "COUPLED" in query:
            return [{"path": "a/hot.ts", "heat": 7}]
        if "defs" in query:
            return [{"name": "get"}]
        return [{"n": {"id": "f1", "name": "run", "repo": "r", "filePath": "a/hot.ts"}, "kind": "Function"}]

    client = MagicMock()
    client.execute_query = run

    nodes = await load_priority_nodes(client, "r", 5)

    assert calls[0] == {"prefix": "file:r:"}
    assert calls[2]["heat"] == {"a/hot.ts": 7} and calls[2]["limit"] == 5
    assert calls[2]["skip_names"] == ["get"]
    assert [(n.id, n.type) for n in nodes] == [("f1", "function")]


async def test_priority_loader_returns_nothing_when_the_graph_errors():
    from strata.ingestion.enrichment import load_priority_nodes

    client = MagicMock()
    client.execute_query = AsyncMock(side_effect=RuntimeError("down"))

    assert await load_priority_nodes(client, "r", 5) == []
