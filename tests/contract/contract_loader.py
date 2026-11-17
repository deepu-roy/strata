"""Helpers shared by the graph-contract tests."""

import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "graph-contract.json"


def load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())
