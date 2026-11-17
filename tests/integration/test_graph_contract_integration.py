"""Contract integration test: ingest the fixtures into a real Neo4j and assert the
resulting graph matches ``contract/graph-contract.json``.

Uses testcontainers (Docker required; skipped otherwise). If Docker via Colima
fails on the Ryuk reaper, run with ``TESTCONTAINERS_RYUK_DISABLED=true``.
Embeddings/summaries are not needed: ingestion does not call any model.
Override the image with ``CODEKG_TEST_NEO4J_IMAGE``.
"""

import asyncio
import os
import re
from pathlib import Path

import pytest

from code_kg.config import Neo4jSettings, Settings
from code_kg.graph.client import Neo4jClient
from code_kg.graph.migrations import run_migrations
from code_kg.ingestion.pipeline import run_pipeline
from tests.contract.contract_loader import load_contract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO = "contract-fixture"
IMAGE = os.environ.get("CODEKG_TEST_NEO4J_IMAGE", "neo4j:5.15")
PASSWORD = "contract-test-pw"

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "datetime": lambda v: hasattr(v, "iso_format") or hasattr(v, "isoformat"),
    "list<integer>[2]": lambda v: isinstance(v, list) and len(v) == 2 and all(isinstance(i, int) for i in v),
    "list<string>": lambda v: isinstance(v, list),
    "list<float>": lambda v: isinstance(v, list),
}


def _check_type(spec: str, value) -> bool:
    key = spec.split(" (")[0].split("|")[0]
    return _TYPE_CHECKS.get(key, lambda v: True)(value)


async def _setup(settings: Settings) -> dict:
    await run_migrations(settings)
    client = Neo4jClient(settings.neo4j)
    await client.connect()
    try:
        result = await run_pipeline(
            client,
            FIXTURES,
            REPO,
            ["**/*.ts","**/*.cs","**/*.md","**/*.py"],
            commit_sha=None,
            do_soft_delete=False,
        )
        assert not result.errors, result.errors
        nodes = await client.execute_query(
            "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"
        )
        rels = await client.execute_query(
            "MATCH (a)-[r]->(b) RETURN a.id AS from_id, labels(a) AS from_labels, type(r) AS type, "
            "b.id AS to_id, labels(b) AS to_labels, properties(r) AS props"
        )
        constraints = await client.execute_query(
            "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
            "WHERE type = 'UNIQUENESS' RETURN name, labelsOrTypes, properties"
        )
        indexes = await client.execute_query(
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties "
            "RETURN name, type, labelsOrTypes, properties"
        )
        return {"nodes": nodes, "rels": rels, "constraints": constraints, "indexes": indexes}
    finally:
        await client.close()


@pytest.fixture(scope="module")
def neo4j_settings():
    try:
        try:
            from testcontainers.community.neo4j import Neo4jContainer
        except ImportError:  # older testcontainers
            try:
            from testcontainers.community.neo4j import Neo4jContainer
        except ImportError:  # older testcontainers
            from testcontainers.neo4j import Neo4jContainer

        container = Neo4jContainer(IMAGE, password=PASSWORD)
        container.start()
    except Exception as exc:  # Docker unavailable / image pull failed
        pytest.skip(f"Neo4j testcontainer unavailable: {exc}")
    try:
        yield Settings(
            neo4j=Neo4jSettings(
                uri=container.get_connection_url(), user="neo4j", password=PASSWORD
            )
        )
    finally:
        container.stop()


@pytest.fixture(scope="module")
def graph(neo4j_settings):
    return asyncio.run(_setup(neo4j_settings))


@pytest.fixture(scope="module")
def contract():
    return load_contract()


def _contract_label_for(contract: dict, labels: list[str]) -> str:
    for name, spec in contract["node_labels"].items():
        if spec["labels"][0] in labels:
            return name
    raise AssertionError(f"labels {labels} not described by the contract")


def test_expected_labels_present(graph, contract):
    present = {l for n in graph["nodes"] for l in n["labels"]}
    assert {"File", "Class", "Interface", "Function", "Document", "Section", "Repo",
            "Layer", "Module", "CodeNode", "DocNode"} <= present
    known = {l for spec in contract["node_labels"].values() for l in spec["labels"]}
    assert present <= known, f"labels not in contract: {present - known}"


def test_nodes_match_contract(graph, contract):
    for node in graph["nodes"]:
        name = _contract_label_for(contract, node["labels"])
        spec = contract["node_labels"][name]
        assert set(node["labels"]) == set(spec["labels"]), (name, node["labels"])
        props = node["props"]
        for prop, typ in spec["required"].items():
            assert prop in props, f"{name} {props.get('id')} missing required {prop}"
            assert _check_type(typ, props[prop]), (name, prop, props[prop])
        allowed = set(spec["required"]) | set(spec["optional"])
        assert set(props) <= allowed, f"{name}: undocumented props {set(props) - allowed}"
        assert props["id"].startswith(spec["id_prefix"]), props["id"]


def test_repo_and_file_path_semantics(graph):
    for node in graph["nodes"]:
        props = node["props"]
        if "Repo" in node["labels"]:
            assert props["id"] == f"repo:{REPO}" and props["name"] == REPO
            continue
        assert props["repo"] == REPO
        fp = props.get("filePath")
        if fp:
            assert not fp.startswith(("/", "./")) and "\\" not in fp, fp
            assert (FIXTURES / fp).is_file(), fp


def test_id_formats(graph, contract):
    for node in graph["nodes"]:
        name = _contract_label_for(contract, node["labels"])
        spec = contract["node_labels"][name]
        props = node["props"]
        nid = props["id"]
        if name == "Repo":
            continue
        assert nid.startswith(f"{spec['id_prefix']}{REPO}:"), nid
        if name in ("File", "Class", "Interface", "Function", "Document", "Section"):
            assert f":{props['filePath']}" in nid, nid
        if name == "Function":
            assert re.fullmatch(rf"function:{REPO}:{re.escape(props['filePath'])}:.+:[0-9a-f]{{4}}", nid)
        if name == "Section":
            assert "#" in nid
        if name == "Class":
            assert nid.endswith(f":{props['name']}")


def test_edges_match_contract(graph, contract):
    seen = set()
    for rel in graph["rels"]:
        spec = contract["edge_types"].get(rel["type"])
        assert spec is not None, f"edge type {rel['type']} not in contract"
        assert spec["emitted_by"] != "never", f"{rel['type']} documented as never emitted"
        assert set(rel["from_labels"]) & set(spec["from"]), (rel["type"], rel["from_labels"])
        assert set(rel["to_labels"]) & set(spec["to"]), (rel["type"], rel["to_labels"])
        for prop in rel["props"]:
            assert prop in spec["properties"], (rel["type"], prop)
        seen.add(rel["type"])
    # Relationships the fixtures are designed to exercise.
    assert {"IN_REPO", "BELONGS_TO_LAYER", "IMPORTS", "HAS_SECTION", "CALLS", "INHERITS",
            "IMPLEMENTS"} <= seen, seen


def test_specific_edges(graph):
    triples = {(r["from_id"], r["type"], r["to_id"]) for r in graph["rels"]}
    ts = f"contract/shapes.ts"
    pol = f"class:{REPO}:{ts}:PoliteGreeter"
    assert (pol, "INHERITS", f"class:{REPO}:{ts}:BaseGreeter") in triples
    assert (pol, "IN_REPO", f"repo:{REPO}") in triples
    assert (f"file:{REPO}:{ts}", "IMPORTS", f"file:{REPO}:contract/util.ts") in triples
    assert (f"file:{REPO}:{ts}", "IMPORTS", f"module:{REPO}:path") in triples
    assert (
        f"class:{REPO}:contract/Shapes.cs:PoliteGreeter",
        "IMPLEMENTS",
        f"interface:{REPO}:contract/Shapes.cs:IGreeter",
    ) in triples
    calls = {(a, c) for a, t, c in triples if t == "CALLS"}
    assert any(a.startswith(f"function:{REPO}:contract/shapes.py:py_greet:")
               and c.startswith(f"function:{REPO}:contract/util_py.py:py_helper:")
               for a, c in calls), calls


def test_constraints_and_indexes_created(graph, contract):
    got = {f"{c['name']}:{c['labelsOrTypes'][0]}.{c['properties'][0]}" for c in graph["constraints"]}
    assert got == set(contract["constraints"]["uniqueness"])
    names = {i["name"] for i in graph["indexes"]}
    expected = {
        entry.split(":")[0]
        for key in ("btree_indexes", "fulltext_indexes", "vector_indexes")
        for entry in contract["constraints"][key]
    }
    assert expected <= names, expected - names


def test_mcp_read_tool_shapes(neo4j_settings, graph, contract):
    from code_kg.domain.models import (
        FindNodesInput, GetNodeInput, ImpactAnalysisInput, GetArchitectureInput,
    )
    from code_kg.mcp.tools import read as R

    tools = contract["mcp"]["tools"]

    async def run():
        client = Neo4jClient(neo4j_settings.neo4j)
        await client.connect()
        try:
            found = await R.find_nodes(
                client,
                FindNodesInput(query="PoliteGreeter", repos=[REPO], use_semantic=False),
                query_embedding=None,
            )
            projects = await R.list_projects(client)
            node = await R.get_node(
                client, GetNodeInput(id=f"class:{REPO}:contract/shapes.ts:PoliteGreeter")
            )
            fn = f"function:{REPO}:contract/shapes.py:py_greet"
            rows = await client.execute_query(
                "MATCH (n:Function) WHERE n.id STARTS WITH $p RETURN n.id AS id", {"p": fn}
            )
            impact = await R.impact_analysis(client, ImpactAnalysisInput(id=rows[0]["id"]))
            arch = await R.get_architecture(client, GetArchitectureInput(repo=REPO))
            return found, projects, node, impact, arch
        finally:
            await client.close()

    found, projects, node, impact, arch = asyncio.run(run())

    assert found.nodes, "find_nodes returned nothing for a known class"
    assert set(found.model_dump()) == set(tools["find_nodes"]["result"]["fields"])
    top = found.nodes[0].model_dump()
    assert set(top) == set(tools["find_nodes"]["result"]["nested"]["nodes[]"])
    # MCP results use snake_case file_path (the graph property is filePath).
    assert all((FIXTURES / n.file_path).is_file() for n in found.nodes)

    assert set(node.model_dump()) == set(tools["get_node"]["result"]["fields"])
    assert {n.edge_type for n in node.neighbors} & {"INHERITS", "IMPLEMENTS", "IN_REPO"}

    assert set(impact.model_dump()) == set(tools["impact_analysis"]["result"]["fields"])
    assert set(arch.model_dump()) == set(tools["get_architecture"]["result"]["fields"])

    summary = {p.slug: p for p in projects.projects}
    assert REPO in summary, "list_projects must report the ingested repo by slug"
    assert summary[REPO].node_count > 0
    assert set(projects.model_dump()["projects"][0]) == set(
        tools["list_projects"]["result"]["nested"]["projects[]"]
    )


def test_cli_bootstrap_and_ingest(neo4j_settings, monkeypatch):
    """The documented consumer path: ``code-kg bootstrap`` then ``code-kg ingest``."""
    from typer.testing import CliRunner

    from code_kg.cli import app

    n = neo4j_settings.neo4j
    monkeypatch.setenv("NEO4J__URI", n.uri)
    monkeypatch.setenv("NEO4J__USER", n.user)
    monkeypatch.setenv("NEO4J__PASSWORD", n.password)
    runner = CliRunner()
    assert runner.invoke(app, ["bootstrap"]).exit_code == 0
    res = runner.invoke(
        app,
        ["ingest", str(FIXTURES / "contract"), "--repo", "cli-fixture",
         "--pattern", "**/*.ts,**/*.cs,**/*.py"],
    )
    assert res.exit_code == 0, res.output

    async def count():
        client = Neo4jClient(n)
        await client.connect()
        try:
            return await client.execute_query(
                "MATCH (r:Repo {id: 'repo:cli-fixture'}) "
                "OPTIONAL MATCH (c:CodeNode {repo: 'cli-fixture'}) "
                "RETURN r.name AS name, count(c) AS nodes"
            )
        finally:
            await client.close()

    row = asyncio.run(count())[0]
    assert row["name"] == "cli-fixture" and row["nodes"] > 0
