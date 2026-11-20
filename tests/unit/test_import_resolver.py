"""Import resolution: relative, index, .js->.ts, tsconfig paths+extends, #src, workspaces, external."""

import json
from pathlib import Path

import pytest

from strata.domain.models import NormalizedNode, RawEdge
from strata.ingestion.import_resolver import ImportResolver
from strata.ingestion.upsert import _resolve_calls_edges, _resolve_import_edges


def _write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, "package.json", json.dumps({"name": "root"}))
    _write(tmp_path, "packages/app/package.json", json.dumps(
        {"name": "@org/app", "imports": {"#src/*": "./build/*"}}))
    _write(tmp_path, "packages/app/tsconfig.base.json", """{
      // comments and trailing commas are legal in tsconfig
      "compilerOptions": { "baseUrl": ".", "paths": { "@a/*": ["src/*"], }, },
    }""")
    _write(tmp_path, "packages/app/tsconfig.json", '{"extends": "./tsconfig.base"}')
    _write(tmp_path, "packages/lib/package.json", json.dumps({"name": "@org/lib"}))
    for f in (
        "packages/app/src/main.ts", "packages/app/src/util.ts", "packages/app/src/comp/index.tsx",
        "packages/app/src/deep/thing.ts", "packages/app/src/internal.ts",
        "packages/lib/src/index.ts", "packages/lib/src/sub/x.ts",
        "packages/app/src/types/context.d.ts", "packages/app/src/types/pkg/index.d.ts",
    ):
        _write(tmp_path, f)
    return tmp_path


FROM = "packages/app/src/main.ts"


@pytest.mark.parametrize(("spec", "expected"), [
    ("./util", "packages/app/src/util.ts"),
    ("./comp", "packages/app/src/comp/index.tsx"),
    ("./util.js", "packages/app/src/util.ts"),
    ("./deep/thing", "packages/app/src/deep/thing.ts"),
    ("@a/deep/thing", "packages/app/src/deep/thing.ts"),
    ("#src/internal.js", "packages/app/src/internal.ts"),
    ("@org/lib", "packages/lib/src/index.ts"),
    ("@org/lib/sub/x", "packages/lib/src/sub/x.ts"),
    ("./types/context", "packages/app/src/types/context.d.ts"),
    ("./types/pkg", "packages/app/src/types/pkg/index.d.ts"),
    ("react", None),
    ("./missing", None),
    ("./style.module.scss", None),
])
def test_resolves_each_specifier_style(repo: Path, spec: str, expected: str | None) -> None:
    assert ImportResolver(repo).resolve(FROM, spec) == expected


def test_without_a_checkout_only_relative_specifiers_resolve() -> None:
    r = ImportResolver(None, {"a/b.ts", "a/c.ts"})

    assert r.resolve("a/b.ts", "./c") == "a/c.ts"
    assert r.resolve("a/b.ts", "@org/lib") is None


def test_incremental_run_still_resolves_to_a_file_outside_the_run(repo: Path) -> None:
    only_changed = {"packages/app/src/main.ts"}

    assert ImportResolver(repo, only_changed).resolve(FROM, "./util") == "packages/app/src/util.ts"


def _imp(path: str) -> RawEdge:
    return RawEdge(type="IMPORTS", from_id=f"file:<repo>:{FROM}", to_id="x",
                   metadata={"import_path": path})  # fmt: skip


def test_import_edges_point_at_files_and_externals_stay_modules(repo: Path) -> None:
    ids = {FROM: f"file:r:{FROM}"}
    edges = _resolve_import_edges(
        [_imp("./util"), _imp("@org/lib"), _imp("react")], "r", ids, ImportResolver(repo, set(ids))
    )

    targets = {t: e.unresolved for e, _, t in edges}
    assert targets == {
        "file:r:packages/app/src/util.ts": False,
        "file:r:packages/lib/src/index.ts": False,
        "module:r:react": True,
    }


def test_calls_prefer_the_file_the_caller_imports() -> None:
    def node(i: str, f: str) -> NormalizedNode:
        return NormalizedNode(id=i, type="function", name="createClient", repo="r", file_path=f)

    caller = NormalizedNode(id="c", type="function", name="run", repo="r", file_path="a.ts")
    nodes = [caller, node("good", "lib.ts"), node("other", "test.ts")]
    call = RawEdge(type="CALLS", from_id="x", to_id="x",
                   metadata={"from_file": "a.ts", "from_method": "run",
                             "call_name": "createClient"})  # fmt: skip

    with_imports = _resolve_calls_edges([call], "r", nodes, {"a.ts": {"lib.ts"}})
    without = _resolve_calls_edges([call], "r", nodes)

    assert [(t, e.unresolved) for e, _, t in with_imports] == [("good", False)]
    assert sorted(t for _, _, t in without) == ["good", "other"]


class _Recorder:
    """Neo4j client stand-in that records every query and reports no rows deleted."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    async def execute_query(self, query, params=None, *a, **k):
        self.queries.append((query, params or {}))
        return [{"n": 0}]

    async def connect(self):
        return None


def _deletes(rec: _Recorder) -> list[str]:
    return [q for q, _ in rec.queries if "DELETE r" in q]


async def test_full_index_replaces_the_repos_imports_and_calls() -> None:
    from strata.ingestion.upsert import normalize_and_upsert

    rec = _Recorder()
    await normalize_and_upsert(rec, [], [], "r", replace_edges=True)

    deleted = _deletes(rec)
    assert any(":File {repo: $repo})-[r:IMPORTS]" in q for q in deleted)
    assert any(":Function {repo: $repo})-[r:CALLS]" in q for q in deleted)
    assert all("MATCH (n)" not in q for q in deleted)  # label-aware: never a full scan


async def test_incremental_index_replaces_only_the_changed_files_imports() -> None:
    from strata.domain.models import RawNode
    from strata.ingestion.upsert import normalize_and_upsert

    rec = _Recorder()
    node = RawNode(type="file", name="a.ts", repo="r", file_path="a.ts", language="typescript")
    await normalize_and_upsert(rec, [node], [], "r", changed_files={"a.ts"}, replace_edges=True)

    deleted = [(q, p) for q, p in rec.queries if "DELETE r" in q]
    assert len(deleted) == 1
    assert "[r:IMPORTS]" in deleted[0][0]
    assert deleted[0][1]["ids"] == ["file:r:a.ts"]


async def test_a_single_file_reindex_never_deletes_edges() -> None:
    from strata.ingestion.upsert import normalize_and_upsert

    rec = _Recorder()
    await normalize_and_upsert(rec, [], [], "r")

    assert _deletes(rec) == []
