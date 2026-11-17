"""Static guards for Cypher that broke the contract on a real Neo4j (no DB needed)."""

import re

from code_kg.graph import queries as Q


def _all_queries() -> dict[str, str]:
    return {k: v for k, v in vars(Q).items() if isinstance(v, str) and "MATCH" in v}


def test_repo_node_has_name_not_slug():
    # Repo nodes are written with `name`; there is no `slug` property.
    assert ".slug" not in Q.LIST_ALL_REPOS
    assert "r.name AS slug" in Q.LIST_ALL_REPOS


def test_no_snake_case_file_path_property_access():
    # The stored property is `filePath`; `n.file_path` is always null.
    for name, cypher in _all_queries().items():
        assert not re.search(r"\b\w+\.file_path\b", cypher), name


def test_no_size_of_pattern():
    # size(<pattern>) was removed in Neo4j 5; use COUNT { } instead.
    for name, cypher in _all_queries().items():
        assert "size((" not in cypher, name


def test_order_by_after_aggregation_uses_projected_vars():
    # ORDER BY r.* after an aggregating RETURN is a syntax error unless r is carried by WITH.
    assert "WITH r, node_count, count(e) AS edge_count" in Q.LIST_ALL_REPOS
