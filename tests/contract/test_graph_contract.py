"""Producer-side contract tests (no Neo4j needed).

Fail when the code's schema (labels, edge types, id builders, constraints, MCP
tools/params/result fields) drifts from ``contract/graph-contract.json``.
Intentional changes must update the contract and bump ``contract_version``.
"""

import asyncio
import re
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from code_kg.config import Neo4jSettings, Settings
from code_kg.domain import ids, models
from code_kg.domain.schema import LABELS, RELATIONSHIPS
from code_kg.graph import migrations

from .contract_loader import load_contract

# ── Structural validity of the contract file itself ──────────────────────────


class _NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[str] = Field(min_length=1)
    id_prefix: str
    id_format: str
    id_example: str
    emitted_by_pipeline: bool
    required: dict[str, str]
    optional: dict[str, str]
    notes: str | None = None


class _EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: list[str] = Field(alias="from")
    to: list[str]
    meaning: str
    emitted_by: str
    properties: dict[str, str]


class _ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stability: str
    kind: str
    params: dict[str, dict[str, Any]]
    result: dict[str, Any]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_name: str
    contract_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    versioning: str
    producer: str
    neo4j: dict[str, str]
    repo_semantics: dict[str, Any]
    file_path_semantics: dict[str, Any]
    node_labels: dict[str, _NodeSpec]
    cross_labels: dict[str, str]
    edge_types: dict[str, _EdgeSpec]
    constraints: dict[str, list[str]]
    mcp: dict[str, Any]
    known_ambiguities: list[str] = Field(min_length=1)


@pytest.fixture(scope="module")
def contract() -> dict:
    return load_contract()


def test_contract_file_is_valid(contract):
    parsed = _Contract.model_validate(contract)
    assert parsed.contract_version
    for name, tool in contract["mcp"]["tools"].items():
        _ToolSpec.model_validate(tool)
        assert tool["stability"] in {"stable", "internal"}, name
        assert tool["kind"] in {"read", "write"}, name
    for name, edge in contract["edge_types"].items():
        assert edge["emitted_by"] in {"ingest", "test-map", "link-docs", "reindex", "never"}, name
    assert isinstance(contract["mcp"]["default_http_port"], int)


# ── Labels, edges ────────────────────────────────────────────────────────────


def test_labels_match_code(contract):
    contract_labels = {
        label for spec in contract["node_labels"].values() for label in spec["labels"]
    } | set(contract["cross_labels"])
    assert contract_labels == set(LABELS.values())


def test_primary_label_is_first_label(contract):
    for name, spec in contract["node_labels"].items():
        assert spec["labels"][0] == name


def test_edge_types_match_code(contract):
    assert set(contract["edge_types"]) == set(RELATIONSHIPS.values())


def test_edge_endpoints_reference_known_labels(contract):
    known = set(LABELS.values())
    for name, edge in contract["edge_types"].items():
        assert set(edge["from"]) <= known, name
        assert set(edge["to"]) <= known, name


# ── Id builders ──────────────────────────────────────────────────────────────

_SAMPLES = {
    "Repo": (ids.make_repo_id, ("myrepo",)),
    "File": (ids.make_file_id, ("myrepo", "src/app.ts")),
    "Class": (ids.make_class_id, ("myrepo", "src/app.ts", "AppService")),
    "Interface": (ids.make_interface_id, ("myrepo", "src/app.ts", "IApp")),
    "Function": (ids.make_function_id, ("myrepo", "src/app.ts", "run", "(a: number)")),
    "Method": (ids.make_method_id, ("myrepo", "src/app.ts", "AppService", "run", "(a: number)")),
    "Enum": (ids.make_enum_id, ("myrepo", "src/a.ts", "Color")),
    "Endpoint": (ids.make_endpoint_id, ("myrepo", "get", "/users")),
    "Document": (ids.make_document_id, ("myrepo", "docs/README.md")),
    "Section": (ids.make_section_id, ("myrepo", "docs/README.md", "installation")),
    "Layer": (ids.make_layer_id, ("myrepo", "service")),
    "Concept": (ids.make_concept_id, ("myrepo", "auth")),
}


def _format_to_regex(fmt: str) -> str:
    out = re.escape(fmt)
    for ph in ("repo", "path", "Name", "name", "Class", "sig4", "VERB", "route", "heading-slug",
               "layer-name", "slug", "import-path"):
        out = out.replace(re.escape(f"<{ph}>"), r"[^<>]+?" if ph != "sig4" else r"[0-9a-f]{4}")
    return f"^{out}$"


@pytest.mark.parametrize("label", sorted(_SAMPLES))
def test_id_builder_matches_contract_format(contract, label):
    spec = contract["node_labels"].get(label)
    if label == "Module":
        pytest.skip("covered separately")
    # Enum is built through make_class_id by the normaliser, but make_enum_id
    # still exists; the contract documents the effective ('class:') prefix.
    assert spec is not None
    builder, args = _SAMPLES[label]
    built = builder(*args)
    if label == "Enum":
        assert built.startswith("enum:")  # builder is unused by the pipeline
        return
    assert built.startswith(spec["id_prefix"]), (label, built)
    assert re.match(_format_to_regex(spec["id_format"]), built), (label, built, spec["id_format"])
    assert re.match(_format_to_regex(spec["id_format"]), spec["id_example"]), label


def test_normalizer_ids_follow_contract(contract):
    from code_kg.ingestion.upsert import normalize_node

    spec = contract["node_labels"]
    cases = {
        "file": ("File", {}),
        "class": ("Class", {}),
        "interface": ("Interface", {}),
        "function": ("Function", {"signature": "(a: number)"}),
        "enum": ("Enum", {}),
        "document": ("Document", {}),
        "section": ("Section", {"extra": {"heading_slug": "intro"}}),
    }
    for raw_type, (label, extra) in cases.items():
        node = models.RawNode(
            type=raw_type, name="X", file_path="src/a.ts", repo="myrepo", **extra
        )
        normalized = normalize_node(node)
        assert normalized is not None
        assert normalized.id.startswith(spec[label]["id_prefix"]), (label, normalized.id)
        assert normalized.repo == "myrepo"
        assert normalized.file_path == "src/a.ts"


# ── Constraints / indexes ────────────────────────────────────────────────────


def test_constraints_match_migrations(contract):
    found = set()
    for stmt in migrations.CONSTRAINTS:
        m = re.search(r"CONSTRAINT (\w+) .*FOR \(n:(\w+)\) REQUIRE n\.(\w+) IS UNIQUE", stmt)
        assert m, stmt
        found.add(f"{m[1]}:{m[2]}.{m[3]}")
    assert found == set(contract["constraints"]["uniqueness"])


def test_indexes_match_migrations(contract):
    btree = set()
    for stmt in migrations.BTREE_INDEXES:
        m = re.search(r"INDEX (\w+) .*FOR \(n:(\w+)\) ON \(n\.(\w+)\)", stmt)
        assert m, stmt
        btree.add(f"{m[1]}:{m[2]}.{m[3]}")
    assert btree == set(contract["constraints"]["btree_indexes"])

    fulltext = set()
    for stmt in migrations.FULLTEXT_INDEXES:
        m = re.search(r"INDEX (\w+) IF NOT EXISTS\s+FOR \(n:(\w+)\) ON EACH \[(.*?)\]", stmt, re.DOTALL)
        assert m, stmt
        props = ",".join(p.strip().removeprefix("n.") for p in m[3].split(","))
        fulltext.add(f"{m[1]}:{m[2]}[{props}]")
    assert fulltext == set(contract["constraints"]["fulltext_indexes"])

    vector = {
        f"{name}:{cfg['label']}.{cfg['property']}({cfg['similarity']})"
        for name, cfg in migrations.VECTOR_INDEX_TEMPLATES.items()
    }
    assert vector == set(contract["constraints"]["vector_indexes"])


# ── MCP surface ──────────────────────────────────────────────────────────────


def _normalise_type(prop: dict) -> str:
    t = prop.get("type") or prop.get("anyOf")
    if isinstance(t, list):
        if any(x.get("type") == "array" for x in t):
            return "array<string>"
        return next(x["type"] for x in t if x.get("type") != "null")
    return t


@pytest.fixture(scope="module")
def mcp_tools() -> dict:
    from code_kg.mcp.server import create_server

    settings = Settings(neo4j=Neo4jSettings(uri="bolt://unused:7687", user="u", password="p"))
    server = create_server(settings)
    tools = asyncio.run(server.list_tools())
    out = {}
    for tool in tools:
        schema = tool.inputSchema
        out[tool.name] = {
            k: {
                "type": _normalise_type(v),
                "required": k in schema.get("required", []),
                "default": v.get("default"),
            }
            for k, v in schema["properties"].items()
        }
    return out


def test_mcp_tool_names_match(contract, mcp_tools):
    assert set(contract["mcp"]["tools"]) == set(mcp_tools)


def test_mcp_tool_params_match(contract, mcp_tools):
    for name, spec in contract["mcp"]["tools"].items():
        assert spec["params"] == mcp_tools[name], name


_RESULT_MODELS = {
    "FindNodesOutput": models.FindNodesOutput,
    "NodeDetail": models.NodeDetail,
    "ImpactAnalysisOutput": models.ImpactAnalysisOutput,
    "FoundNode": models.FoundNode,
    "ListProjectsOutput": models.ListProjectsOutput,
    "ArchitectureOutput": models.ArchitectureOutput,
    "DeadCodeOutput": models.DeadCodeOutput,
    "IngestRepoOutput": models.IngestRepoOutput,
    "StartIngestOutput": models.StartIngestOutput,
    "IngestJobSnapshot": models.IngestJobSnapshot,
    "CheckRepoChangesOutput": models.CheckRepoChangesOutput,
}


def test_mcp_result_fields_match_models(contract):
    for name, tool in contract["mcp"]["tools"].items():
        result = tool["result"]
        model = _RESULT_MODELS.get(result.get("model") or "")
        if model is None:
            continue
        assert set(result["fields"]) == set(model.model_fields), name
    nested = {
        ("find_nodes", "nodes[]"): models.FoundNode,
        ("get_node", "neighbors[]"): models.NeighborRef,
        ("impact_analysis", "target"): models.FoundNode,
        ("impact_analysis", "upstream[]"): models.ImpactPath,
        ("impact_analysis", "downstream[]"): models.ImpactPath,
        ("impact_analysis", "tests[]"): models.FoundNode,
        ("impact_analysis", "docs[]"): models.FoundNode,
        ("list_projects", "projects[]"): models.ProjectSummary,
        ("get_architecture", "hotspots[]"): models.HotspotNode,
        ("check_repo_changes", "files[]"): models.ChangedFile,
    }
    for (tool, key), model in nested.items():
        got = contract["mcp"]["tools"][tool]["result"]["nested"][key]
        assert set(got) == set(model.model_fields), (tool, key)


def test_default_mcp_port_matches_settings(contract):
    assert contract["mcp"]["default_http_port"] == Settings.model_fields["mcp_http_port"].default


def test_doc_mentions_contract_version(contract):
    from .contract_loader import CONTRACT_PATH

    doc = (CONTRACT_PATH.parents[1] / "docs" / "graph-contract.md").read_text()
    assert f"**{contract['contract_version']}**" in doc
