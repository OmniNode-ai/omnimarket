"""The checked-in relation inventory lists the trigger as an overlay_store reader."""

from __future__ import annotations

import json
from pathlib import Path

INVENTORY = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "evidence"
    / "OMN-15423-relation-inventory.json"
)


def _find_relations(node: object, name: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(node, dict):
        if node.get("name") == name and "readers" in node:
            found.append(node)
        for value in node.values():
            found.extend(_find_relations(value, name))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_relations(value, name))
    return found


def test_trigger_node_is_a_listed_reader_of_overlay_store() -> None:
    relations = _find_relations(json.loads(INVENTORY.read_text()), "overlay_store")
    assert relations
    assert any(
        "node_delegation_check_regression_effect" in r["readers"]  # type: ignore[operator]
        for r in relations
    )
