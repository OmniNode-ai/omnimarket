# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear snapshot carries each issue's parent and its project id, so the work list can read scope up the chain (OMN-23184)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    HandlerLinearSnapshot,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelLinearSnapshotRequest,
)
from tests.nodes.node_linear_snapshot_effect.test_snapshot_json_shape import (
    VIEWER,
    Pages,
    node,
)

pytestmark = pytest.mark.unit

FACTORY_ID = "ae87f221-ad60-49fe-aeae-b4417960ff90"


def _write(tmp_path: Path, *nodes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    HandlerLinearSnapshot(Pages(list(nodes))).handle(
        ModelLinearSnapshotRequest(out_dir=str(tmp_path), date="2026-11-03")
    )
    data = json.loads((tmp_path / "linear-2026-11-03.json").read_text("utf-8"))
    return {i["id"]: i for i in data["issues"]}


def test_the_query_asks_for_the_parent_and_the_project_id() -> None:
    for query in (handler_linear_snapshot.QUERY, handler_linear_snapshot.QUERY_CHANGES):
        assert "parent { identifier }" in query
        assert "project { id name }" in query


def test_the_file_carries_parent_id_and_project_id(tmp_path: Path) -> None:
    issues = _write(
        tmp_path,
        node(
            23229,
            parent={"identifier": "OMN-23127"},
            project={"id": FACTORY_ID, "name": "Factory"},
        ),
        node(22478, project=None),
    )
    assert issues["OMN-23229"]["parentId"] == "OMN-23127"
    assert issues["OMN-23229"]["projectId"] == FACTORY_ID
    assert issues["OMN-23229"]["project"] == "Factory"
    assert issues["OMN-22478"]["parentId"] is None
    assert issues["OMN-22478"]["projectId"] == ""


def test_a_snapshot_file_from_before_the_keys_still_reads() -> None:
    old = {
        "operator": VIEWER,
        "issues": [
            {
                "id": "OMN-1",
                "title": "t",
                "status": "Todo",
                "statusType": "unstarted",
                "assignee": VIEWER,
                "updatedAt": "2026-11-03T14:00:00.000Z",
            }
        ],
    }
    (issue,) = ModelLinearSnapshot.model_validate(old).issues
    assert issue.parent_id is None
    assert issue.project_id == ""
