# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the Linear snapshot node's document and the floor's reading of its child ids (OMN-22934).

``node_linear_snapshot_effect`` writes ``linear-<date>.json`` from its typed model; ``node_factory_floor_effect`` reads the
same file with ``load_linear`` and the stopped-line scope walks the child ids it keeps. The test writes the file with the
snapshot node's own model and reads it with the floor's loader, so a rename or reshape of ``children`` on either side
reads red here. The file is the seam; no topic carries it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_compute.handlers.floor_stopped import (
    build_stopped,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_linear
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelSnapshotIssue,
)
from tests.nodes.node_factory_floor_compute.test_stopped_lane_child_tickets import (
    CHILD,
    ROOT,
    floor_request,
)

pytestmark = pytest.mark.unit

OPERATOR = "user-1"


def snapshot_issue(ticket: str, children: tuple[str, ...] | None) -> ModelSnapshotIssue:
    return ModelSnapshotIssue(
        id=ticket,
        title=f"title of {ticket}",
        status="In Progress",
        statusType="started",
        assignee=OPERATOR,
        updatedAt="2026-11-10T01:00:00.000Z",
        children=children,
    )


def written(tmp_path: Path, *issues: ModelSnapshotIssue) -> Path:
    path = tmp_path / "linear-2026-11-10.json"
    document = ModelLinearSnapshot(operator=OPERATOR, issues=issues)
    path.write_text(document.model_dump_json(by_alias=True), encoding="utf-8")
    return path


def test_golden_the_floor_reads_the_child_ids_the_snapshot_node_wrote(
    tmp_path: Path,
) -> None:
    path = written(tmp_path, snapshot_issue(ROOT, (CHILD,)), snapshot_issue(CHILD, ()))
    issues = {i.id: i for i in load_linear(path)}
    assert issues[ROOT].children == (CHILD,)
    assert issues[CHILD].children == ()
    stopped, _skipped, _cause = build_stopped(floor_request(tuple(issues.values())))
    assert [s.lane for s in stopped] == ["labrunpool-1050"]


def test_error_chain_a_snapshot_that_did_not_read_children_lists_no_child_lane(
    tmp_path: Path,
) -> None:
    """``children`` null is "not read" in the snapshot node's model: the floor takes no ancestry from it."""
    path = written(tmp_path, snapshot_issue(ROOT, None), snapshot_issue(CHILD, None))
    issues = load_linear(path)
    assert all(i.children == () for i in issues)
    assert build_stopped(floor_request(issues))[0] == ()


def test_break_a_snapshot_whose_child_key_is_renamed_reads_no_children(
    tmp_path: Path,
) -> None:
    path = written(tmp_path, snapshot_issue(ROOT, (CHILD,)))
    broken = path.read_text(encoding="utf-8").replace('"children"', '"child_ids"')
    path.write_text(broken, encoding="utf-8")
    assert all(i.children == () for i in load_linear(path))
    assert build_stopped(floor_request(load_linear(path)))[0] == ()
