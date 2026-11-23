# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26435: the checked-in relation inventory lists the freshness alert node's done-items coverage reads. #6601 declared a coverage check on node_projection_freshness_alert_effect that reads done_items and work_ledger_rows, but the checked-in inventory was not regenerated, so dev's Tests (Split 7/20) failed; this pins the regenerated entries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

INVENTORY = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "evidence"
    / "OMN-15423-relation-inventory.json"
)
NODE = "node_projection_freshness_alert_effect"
CONTRACT = "src/omnimarket/nodes/node_projection_freshness_alert_effect/contract.yaml"


def _relation(name: str) -> dict:
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    matches = [
        relation
        for relation in data["relations"]
        if relation.get("name") == name
        and relation.get("target_schema") == "omninode_internal"
    ]
    assert len(matches) == 1, (
        f"expected exactly one relation named {name!r} with "
        f"target_schema 'omninode_internal', found {len(matches)}"
    )
    return matches[0]


@pytest.mark.parametrize("relation", ["done_items", "work_ledger_rows"])
def test_freshness_alert_node_is_listed_for_its_coverage_reads(relation: str) -> None:
    entry = _relation(relation)
    assert NODE in entry["readers"], f"{NODE} missing from readers of {relation}"
    assert NODE in entry["accessor_nodes"], (
        f"{NODE} missing from accessor_nodes of {relation}"
    )
    assert CONTRACT in entry["contract_sources"], (
        f"{CONTRACT} missing from contract_sources of {relation}"
    )
