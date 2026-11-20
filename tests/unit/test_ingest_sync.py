"""Sync, auth, jobs/progress and change-detection tests (local temp git repos, mocked Neo4j)."""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strata.domain.models import (
    CheckRepoChangesInput,
    IngestRepoInput,
    IngestRepoOutput,
)
from strata.ingestion import git as gitmod
from strata.ingestion.git import GitError, clone_dir_name, redact, sync_repo
from strata.ingestion.jobs import REGISTRY, IngestProgress, JobRegistry
from strata.ingestion.pipeline import run_pipeline
from strata.mcp.tools import changes as changes_mod
from strata.mcp.tools import write as write_mod
from strata.mcp.tools.changes import check_repo_changes
from strata.mcp.tools.write import get_ingest_job, ingest_repo, start_ingest_repo

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class Upstream:
    """A bare 'upstream' plus a developer clone that pushes to it."""

    def __init__(self, root: Path, name: str = "shop") -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.bare = root / f"{name}.git"
        self.dev = root / f"{name}-dev"
        git(root, "init", "--bare", "-b", "main", str(self.bare))
        git(root, "clone", str(self.bare), str(self.dev))
        git(self.dev, "checkout", "-b", "main")

    @property
    def url(self) -> str:
        return str(self.bare)

    def commit(self, files: dict[str, str | None], msg: str = "c") -> str:
        for rel, content in files.items():
            p = self.dev / rel
            if content is None:
                git(self.dev, "rm", "-q", rel)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            git(self.dev, "add", rel)
        git(self.dev, "commit", "-q", "-m", msg)
        git(self.dev, "push", "-q", "-f", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _fresh_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture
def settings(tmp_path):
    s = MagicMock()
    s.workdir = str(tmp_path / "workdir")
    return s


@pytest.fixture
def remote(monkeypatch):
    """Treat filesystem URLs as *remote* so the sync path is exercised."""
    monkeypatch.setattr(write_mod, "_is_local_path", lambda u: False)
    monkeypatch.setattr(changes_mod, "_is_local_path", lambda u: False)


@pytest.fixture
def pipeline_mocks():
    """Mock the Neo4j-touching pipeline steps; record what was upserted."""
    rec = {"upserts": [], "meta": []}

    async def fake_upsert(client, nodes, edges, repo, changed_files=None, seen_commit=None,
                          repo_path=None, replace_edges=False):
        rec["upserts"].append(
            {"files": {n.file_path for n in nodes}, "changed_files": changed_files}
        )
        return len(nodes), len(edges)

    async def fake_meta(client, slug, commit_sha=None, **kw):
        rec["meta"].append(commit_sha)

    with patch("strata.ingestion.pipeline.normalize_and_upsert", new=fake_upsert), \
         patch("strata.ingestion.pipeline.update_repo_meta", new=fake_meta), \
         patch("strata.ingestion.pipeline.soft_delete_orphans", new=AsyncMock(return_value=0)), \
         patch.object(write_mod, "soft_delete_file_nodes", new=AsyncMock()), \
         patch.object(write_mod, "upsert_renamed_from_edge", new=AsyncMock(return_value=True)):
        yield rec


def _inp(url, **kw):
    kw.setdefault("repo_slug", "shop")
    kw.setdefault("patterns", ["**/*.py"])
    return IngestRepoInput(repo_url=url, **kw)


# ── redact ────────────────────────────────────────────────────────────────────


class TestRedact:
    def test_url_credentials(self):
        assert redact("clone https://bob:s3cret@github.com/x/y.git now") == \
            "clone https://***@github.com/x/y.git now"
        assert "tok" not in redact("https://tok@github.com/x/y")

    def test_header_and_literals(self):
        assert "QUJD" not in redact("Authorization: Basic QUJD")
        assert redact("leaked hunter2 here", ["hunter2"]) == "leaked *** here"

    def test_plain_text_untouched(self):
        assert redact("git@github.com:x/y.git") == "git@github.com:x/y.git"
        assert redact("") == ""


# ── sync_repo ─────────────────────────────────────────────────────────────────


class TestSyncRepo:
    def test_clone_dir_name_is_unique_per_url(self, tmp_path):
        a = Upstream(tmp_path / "a")
        b = Upstream(tmp_path / "b")
        na, nb = clone_dir_name(a.url), clone_dir_name(b.url)
        assert na != nb and na.startswith("shop-") and nb.startswith("shop-")
        assert clone_dir_name("https://GitHub.com/x/Shop.git/") == \
            clone_dir_name("https://github.com/x/Shop")
        assert clone_dir_name("https://u:pw@github.com/x/shop") == \
            clone_dir_name("https://github.com/x/shop")

    def test_same_named_repos_do_not_collide(self, tmp_path):
        a, b = Upstream(tmp_path / "a"), Upstream(tmp_path / "b")
        sha_a = a.commit({"only_a.py": "x = 1\n"})
        sha_b = b.commit({"only_b.py": "y = 2\n"})
        wd = tmp_path / "wd"
        pa, ha = sync_repo(a.url, wd)
        pb, hb = sync_repo(b.url, wd)
        assert pa != pb
        assert (ha, hb) == (sha_a, sha_b)
        assert (pa / "only_a.py").exists() and not (pa / "only_b.py").exists()
        assert (pb / "only_b.py").exists() and not (pb / "only_a.py").exists()

    def test_second_sync_checks_out_new_upstream_commit(self, tmp_path):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "a = 1\n"})
        wd = tmp_path / "wd"
        path, _ = sync_repo(up.url, wd)
        new = up.commit({"b.py": "b = 1\n"})
        path2, head = sync_repo(up.url, wd)
        assert path2 == path and head == new
        assert (path / "b.py").exists()
        assert git(path, "rev-parse", "HEAD") == new

    def test_commit_sha_optional_resolves_branch_tip_and_pin_works(self, tmp_path):
        up = Upstream(tmp_path / "u")
        first = up.commit({"a.py": "a = 1\n"})
        tip = up.commit({"b.py": "b = 1\n"})
        wd = tmp_path / "wd"
        _, head = sync_repo(up.url, wd, branch="main")
        assert head == tip
        path, pinned = sync_repo(up.url, wd, commit_sha=first)
        assert pinned == first and not (path / "b.py").exists()
        with pytest.raises(GitError):
            sync_repo(up.url, wd, commit_sha="0" * 40)
        with pytest.raises(GitError):
            sync_repo(up.url, wd, branch="nope")

    def test_dirty_tree_is_cleaned(self, tmp_path):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "a = 1\n"})
        path, _ = sync_repo(up.url, tmp_path / "wd")
        (path / "junk.py").write_text("x")
        (path / "a.py").write_text("tampered")
        sync_repo(up.url, tmp_path / "wd")
        assert not (path / "junk.py").exists()
        assert (path / "a.py").read_text() == "a = 1\n"

    def test_clone_or_fetch_back_compat(self, tmp_path):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "a = 1\n"})
        p = gitmod.clone_or_fetch(up.url, tmp_path / "wd")
        assert (p / "a.py").exists()
        up.commit({"b.py": "b"})
        assert (gitmod.clone_or_fetch(up.url, tmp_path / "wd") / "b.py").exists()

    def test_checkout_false_leaves_tree(self, tmp_path):
        up = Upstream(tmp_path / "u")
        old = up.commit({"a.py": "a = 1\n"})
        path, _ = sync_repo(up.url, tmp_path / "wd")
        new = up.commit({"b.py": "b"})
        _, head = sync_repo(up.url, tmp_path / "wd", checkout=False)
        assert head == new and git(path, "rev-parse", "HEAD") == old


# ── auth ──────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_token_goes_to_env_header_only(self, monkeypatch):
        monkeypatch.delenv("STRATA_GIT_USER", raising=False)
        env, secrets = gitmod._auth_env("https://github.com/x/y", "tok-123")
        assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
        import base64
        assert env["GIT_CONFIG_VALUE_0"] == \
            "Authorization: Basic " + base64.b64encode(b"x-access-token:tok-123").decode()
        assert "tok-123" in secrets

    def test_env_vars_and_non_http(self, monkeypatch):
        monkeypatch.setenv("STRATA_GIT_TOKEN", "envtok")
        monkeypatch.setenv("STRATA_GIT_USER", "bob")
        env, _ = gitmod._auth_env("https://h/x", None)
        import base64
        assert base64.b64encode(b"bob:envtok").decode() in env["GIT_CONFIG_VALUE_0"]
        assert gitmod._auth_env("git@github.com:x/y.git", None) == ({}, [])
        assert gitmod._auth_env("/local/path", "t") == ({}, [])

    def test_token_never_in_argv_errors_or_logs(self, tmp_path, caplog):
        token = "ghp_SUPERSECRETTOKEN"
        import base64
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        calls = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            calls.append((list(cmd), dict(kw.get("env") or {})))
            return real_run(cmd, **kw)

        caplog.set_level(logging.DEBUG)
        with patch.object(gitmod.subprocess, "run", side_effect=spy), \
                pytest.raises(GitError) as ei:
            sync_repo("https://user:pw123@127.0.0.1:1/x/shop.git",
                      tmp_path / "wd", token=token)
        assert calls
        for cmd, _ in calls:
            joined = " ".join(cmd)
            assert token not in joined and b64 not in joined
        # the credential is delivered through the environment instead
        assert any(token not in v and b64 in v
                   for _, env in calls for v in env.values())
        blob = str(ei.value) + caplog.text
        assert token not in blob and b64 not in blob and "pw123" not in blob


# ── ingest_repo: stale checkout regression, notes, optional sha ────────────────


class TestIngestSync:
    async def test_second_ingest_indexes_new_upstream_file_and_records_head(
        self, tmp_path, settings, remote, pipeline_mocks
    ):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "def a():\n    return 1\n"})
        out1 = await ingest_repo(MagicMock(), settings, _inp(up.url))
        assert out1.status == "completed", out1.errors
        assert "a.py" in pipeline_mocks["upserts"][-1]["files"]

        new = up.commit({"b.py": "def b():\n    return 2\n"})
        out2 = await ingest_repo(MagicMock(), settings, _inp(up.url))
        assert out2.status == "completed", out2.errors
        assert "b.py" in pipeline_mocks["upserts"][-1]["files"]
        assert pipeline_mocks["meta"][-1] == new
        assert out2.commit_sha == new

    async def test_commit_sha_optional_and_branch_honoured(
        self, tmp_path, settings, remote, pipeline_mocks
    ):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "def a():\n    pass\n"})
        git(up.dev, "checkout", "-q", "-b", "dev")
        (up.dev / "d.py").write_text("def d():\n    pass\n")
        git(up.dev, "add", "d.py")
        git(up.dev, "commit", "-q", "-m", "d")
        dev_sha = git(up.dev, "rev-parse", "HEAD")
        git(up.dev, "push", "-q", "origin", "dev")
        out = await ingest_repo(MagicMock(), settings, _inp(up.url, branch="dev"))
        assert out.commit_sha == dev_sha and pipeline_mocks["meta"][-1] == dev_sha
        assert "d.py" in pipeline_mocks["upserts"][-1]["files"]

    async def test_unreachable_base_falls_back_to_full_with_note(
        self, tmp_path, settings, remote, pipeline_mocks
    ):
        up = Upstream(tmp_path / "u")
        c1 = up.commit({"a.py": "def a():\n    pass\n"})
        old = up.commit({"b.py": "def b():\n    pass\n"})
        await ingest_repo(MagicMock(), settings, _inp(up.url))
        # force-push: rewrite history so `old` is no longer reachable
        git(up.dev, "reset", "-q", "--hard", c1)
        new = up.commit({"c.py": "def c():\n    pass\n"})
        out = await ingest_repo(
            MagicMock(), settings, _inp(up.url, base_commit_sha=old)
        )
        assert out.status == "completed", out.errors
        assert any("not reachable" in n for n in out.notes)
        last = pipeline_mocks["upserts"][-1]
        assert last["changed_files"] is None          # full, not incremental
        assert {"a.py", "c.py"} <= last["files"]
        assert out.commit_sha == new

    async def test_unknown_base_sha_falls_back(
        self, tmp_path, settings, remote, pipeline_mocks
    ):
        up = Upstream(tmp_path / "u")
        up.commit({"a.py": "def a():\n    pass\n"})
        out = await ingest_repo(
            MagicMock(), settings, _inp(up.url, base_commit_sha="f" * 40)
        )
        assert out.status == "completed" and out.notes

    async def test_reachable_base_stays_incremental(
        self, tmp_path, settings, remote, pipeline_mocks
    ):
        up = Upstream(tmp_path / "u")
        base = up.commit({"a.py": "def a():\n    pass\n"})
        up.commit({"b.py": "def b():\n    pass\n"})
        out = await ingest_repo(
            MagicMock(), settings, _inp(up.url, base_commit_sha=base)
        )
        assert out.notes == []
        last = pipeline_mocks["upserts"][-1]
        assert last["changed_files"] == {"b.py"}

    async def test_sync_failure_reports_failed_without_credentials(
        self, tmp_path, settings, remote
    ):
        out = await ingest_repo(
            MagicMock(), settings,
            _inp("https://user:pw999@127.0.0.1:1/x/shop.git"),
        )
        assert out.status == "failed"
        assert "pw999" not in " ".join(out.errors)


# ── jobs ──────────────────────────────────────────────────────────────────────


def _ok_output(job_id="x", **kw):
    return IngestRepoOutput(
        job_id=job_id, status="completed", nodes_added=5, nodes_modified=1,
        nodes_removed=2, edges_added=7, edges_removed=0, duration_seconds=0.1, **kw,
    )


async def _drain():
    await asyncio.gather(*list(write_mod._BACKGROUND_TASKS), return_exceptions=True)


class TestJobs:
    async def test_lifecycle_and_progress(self, settings):
        gate_mid, gate_end = asyncio.Event(), asyncio.Event()
        reached = asyncio.Event()

        async def fake_core(client, st, inp, job_id, progress):
            progress.set_phase("cloning")
            progress.set_commit("a" * 40)
            progress.set_phase("parsing")
            progress.set_total(10)
            progress.file_done(3)
            reached.set()
            await gate_mid.wait()
            progress.file_done(4)
            progress.set_phase("upserting")
            await gate_end.wait()
            return _ok_output(job_id, notes=["hello"], commit_sha="a" * 40)

        with patch.object(write_mod, "_run_ingest", new=fake_core):
            started = await start_ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            assert started.status == "queued" and started.already_running is False
            snap0 = get_ingest_job(started.job_id)
            assert snap0.status in ("queued", "running")

            await reached.wait()
            snap1 = get_ingest_job(started.job_id)
            assert (snap1.status, snap1.phase) == ("running", "parsing")
            assert (snap1.files_done, snap1.files_total) == (3, 10)

            # one active job per repo_slug
            again = await start_ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            assert again.already_running is True and again.job_id == started.job_id
            other = await start_ingest_repo(
                MagicMock(), settings, _inp("https://h/x/other", repo_slug="other")
            )
            assert other.job_id != started.job_id and other.already_running is False
            blocking = await ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            assert blocking.status == "queued" and blocking.job_id == started.job_id

            gate_mid.set()
            await asyncio.sleep(0.01)
            snap2 = get_ingest_job(started.job_id)
            assert snap2.files_done == 7 and snap2.phase == "upserting"

            gate_end.set()
            await _drain()
            done = get_ingest_job(started.job_id)
            assert done.status == "completed" and done.phase == "done"
            assert done.files_done == done.files_total == 10
            assert (done.nodes_added, done.edges_added, done.nodes_removed) == (5, 7, 2)
            assert done.commit_sha == "a" * 40 and done.notes == ["hello"]
            assert done.finished_at and done.elapsed_seconds >= 0

            # finished => a new job may start for the same slug
            gate_end.set()
            nxt = await start_ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            assert nxt.job_id != started.job_id and nxt.already_running is False
            await _drain()

    async def test_failed_job_captures_exception(self, settings):
        async def boom(*a, **k):
            raise RuntimeError("kaboom https://u:tok@h/x")

        with patch.object(write_mod, "_run_ingest", new=boom):
            started = await start_ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            await _drain()
        snap = get_ingest_job(started.job_id)
        assert snap.status == "failed" and snap.phase == "done"
        assert any("kaboom" in e for e in snap.errors)
        assert not any("tok" in e for e in snap.errors)

    async def test_failed_output_marks_job_failed(self, settings):
        async def core(client, st, inp, job_id, progress):
            return write_mod._failed(job_id, "bad thing")

        with patch.object(write_mod, "_run_ingest", new=core):
            started = await start_ingest_repo(MagicMock(), settings, _inp("https://h/x/shop"))
            await _drain()
        snap = get_ingest_job(started.job_id)
        assert snap.status == "failed" and snap.errors == ["bad thing"]

    def test_unknown_job_is_none(self):
        assert get_ingest_job("nope") is None

    async def test_real_pipeline_reports_file_progress(self, tmp_path, settings, pipeline_mocks):
        for i in range(3):
            (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
        out = await ingest_repo(MagicMock(), settings, _inp(str(tmp_path), commit_sha="abc123"))
        assert out.status == "completed", out.errors
        snap = get_ingest_job(out.job_id)
        assert snap.files_total == 3 and snap.files_done == 3
        assert snap.status == "completed" and snap.commit_sha == "abc123"

    async def test_pipeline_progress_sequence(self, tmp_path, pipeline_mocks):
        for i in range(3):
            (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
        events = []

        class Rec(IngestProgress):
            def set_phase(self, p): events.append(("phase", p))
            def set_total(self, n): events.append(("total", n))
            def file_done(self, n=1): events.append(("done", n))

        await run_pipeline(MagicMock(), tmp_path, "s", ["**/*.py"], commit_sha="c",
                           progress=Rec())
        kinds = [e for e in events if e[0] != "done"]
        assert kinds[0] == ("phase", "parsing") and ("total", 3) in kinds
        assert [e[1] for e in kinds if e[0] == "phase"] == \
            ["parsing", "upserting", "cleanup", "mapping_tests"]
        assert sum(n for k, n in events if k == "done") >= 3
        assert events.index(("total", 3)) < events.index(next(e for e in events if e[0] == "done"))

    async def test_pipeline_maps_tests_and_survives_a_mapping_failure(
        self, tmp_path, pipeline_mocks, monkeypatch
    ):
        (tmp_path / "m.py").write_text("def f():\n    return 1\n")
        calls = []

        async def boom(client, slug, **kw):
            calls.append(slug)
            raise RuntimeError("neo4j hiccup")

        monkeypatch.setattr("strata.ingestion.tests_mapper.map_tests", boom)

        result = await run_pipeline(MagicMock(), tmp_path, "s", ["**/*.py"], commit_sha="c")

        assert calls == ["s"]
        assert result.errors == []  # the index stands; test-map can be re-run on its own

    def test_registry_ttl_and_cap(self):
        reg = JobRegistry(ttl=0.0, max_jobs=2)
        j1, _ = reg.start("a")
        j1.finish("completed")
        j1.finished_at -= 1
        j2, _ = reg.start("b")          # prunes expired j1
        assert reg.get(j1.job_id) is None and reg.get(j2.job_id) is j2
        reg2 = JobRegistry(ttl=3600, max_jobs=2)
        made = []
        for slug in "abcd":
            j, _ = reg2.start(slug)
            j.finish("completed")
            made.append(j)
        reg2.start("e")
        assert len(reg2._jobs) <= 3 and reg2.get(made[0].job_id) is None


# ── check_repo_changes ────────────────────────────────────────────────────────


def _chk(url, **kw):
    kw.setdefault("repo_slug", "shop")
    kw.setdefault("patterns", ["**/*.py"])
    return CheckRepoChangesInput(repo_url=url, **kw)


def _meta(sha):
    return patch.object(
        changes_mod, "get_repo_meta",
        new=AsyncMock(return_value=None if sha is None else {"last_commit_sha": sha}),
    )


class TestCheckRepoChanges:
    async def test_never_indexed(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        head = up.commit({"a.py": "a = 1\n"})
        with _meta(None):
            r = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert r.indexed_sha is None and r.head_sha == head
        assert r.requires_full and not r.up_to_date and r.files == []

    async def test_up_to_date(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        head = up.commit({"a.py": "a = 1\n"})
        with _meta(head):
            r = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert r.up_to_date and not r.requires_full
        assert (r.added, r.modified, r.deleted, r.renamed, r.new_commits) == (0, 0, 0, 0, 0)
        assert r.files == [] and r.truncated is False

    async def test_added_modified_deleted_renamed(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        body = "def long_enough_body():\n" + "    x = 1\n" * 30
        base = up.commit({
            "mod.py": "v = 1\n", "gone.py": "g = 1\n", "old_name.py": body,
            "readme.txt": "x",
        })
        # first sync so the clone exists, then upstream moves on
        with _meta(base):
            await check_repo_changes(MagicMock(), settings, _chk(up.url))
        git(up.dev, "mv", "old_name.py", "new_name.py")
        up.commit({
            "mod.py": "v = 2\n", "gone.py": None, "add.py": "n = 1\n",
            "readme.txt": "changed", "node_modules/x/y.py": "z = 1\n",
        })
        with _meta(base):
            r = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert not r.up_to_date and not r.requires_full
        assert (r.added, r.modified, r.deleted, r.renamed) == (1, 1, 1, 1)
        assert r.new_commits == 1 and r.indexed_sha == base
        by_status = {f.status: f.path for f in r.files}
        assert by_status == {
            "added": "add.py", "modified": "mod.py",
            "deleted": "gone.py", "renamed": "new_name.py",
        }

    async def test_truncates_file_list(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        base = up.commit({"a.py": "a = 1\n"})
        up.commit({f"f{i}.py": f"v = {i}\n" for i in range(205)})
        with _meta(base):
            r = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert r.added == 205 and len(r.files) == 200 and r.truncated is True

    async def test_unreachable_base_requires_full(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        c1 = up.commit({"a.py": "a = 1\n"})
        old = up.commit({"b.py": "b = 1\n"})
        with _meta(old):
            await check_repo_changes(MagicMock(), settings, _chk(up.url))
        git(up.dev, "reset", "-q", "--hard", c1)
        up.commit({"c.py": "c = 1\n"})
        with _meta(old):
            r = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert r.requires_full and not r.up_to_date and r.files == []
        with _meta("f" * 40):
            r2 = await check_repo_changes(MagicMock(), settings, _chk(up.url))
        assert r2.requires_full

    async def test_does_not_touch_working_tree(self, tmp_path, settings, remote):
        up = Upstream(tmp_path / "u")
        base = up.commit({"a.py": "a = 1\n"})
        with _meta(base):
            await check_repo_changes(MagicMock(), settings, _chk(up.url))
        up.commit({"b.py": "b = 1\n"})
        with _meta(base):
            await check_repo_changes(MagicMock(), settings, _chk(up.url))
        clone = Path(settings.workdir) / clone_dir_name(up.url)
        assert git(clone, "rev-parse", "HEAD") == base


class TestExcludePatterns:
    def test_is_excluded_matches_paths_and_base_names(self):
        from strata.ingestion.pipeline import is_excluded

        pats = ["**/generated/**", "*.min.js", "docs/*"]
        assert is_excluded("src/generated/a.ts", pats)
        assert is_excluded("web/app.min.js", pats)
        assert is_excluded("docs/guide.js", pats)
        assert not is_excluded("src/app.ts", pats)
        assert not is_excluded("src/app.ts", None)

    def test_collect_files_applies_excludes_to_walks_and_explicit_lists(self, tmp_path):
        from strata.ingestion.pipeline import collect_files

        for name in ("a.ts", "gen/b.ts", "c.min.ts"):
            f = tmp_path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("export const x = 1\n")

        walked = collect_files(tmp_path, ["**/*.ts"], exclude_patterns=["gen/*", "*.min.ts"])
        explicit = collect_files(
            tmp_path, ["**/*.ts"], explicit_files=["a.ts", "gen/b.ts"], exclude_patterns=["gen/*"]
        )

        assert walked == ["a.ts"]
        assert explicit == ["a.ts"]
