"""Pytest configuration and shared fixtures."""

import pytest


@pytest.fixture
def sample_raw_node():
    """Sample RawNode for testing."""
    from strata.domain.models import RawNode
    return RawNode(
        type="class",
        name="TestClass",
        language="typescript",
        file_path="src/test.ts",
        repo="test-repo",
    )
