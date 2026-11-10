# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear snapshot writer's JSON carries the fields every reader needs, validated by one typed model."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    HandlerLinearSnapshot,
    Post,
    SnapshotReadError,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelLinearSnapshotRequest,
    ModelLinearSnapshotResult,
)
from omnimarket.nodes.node_linear_snapshot_effect.runtime_linear_snapshot import main
from omnimarket.nodes.node_plans_today_effect.handlers.handler_plans_today import (
    load_linear,
)
from omnimarket.nodes.node_plans_today_effect.models.model_plans_today import (
    ModelLinearSidecar,
)

VIEWER = "7f1c0f6e-0000-4000-8000-000000000001"


def node(
    n: int, *, description: str | None = "Body of the ticket.", **over: Any
) -> dict[str, Any]:
    base = {
        "identifier": f"OMN-{n}",
        "title": f"Ticket {n}",
        "description": description,
        "updatedAt": "2026-11-03T14:00:00.000Z",
        "priority": 2,
        "priorityLabel": "High",
        "state": {"name": "In Progress", "type": "started"},
        "assignee": {"id": VIEWER},
        "labels": {"nodes": [{"name": "delegation"}, {"name": "machinery"}]},
        "project": {"name": "Delegation Pillar"},
    }
    return {**base, **over}


class Pages:
    """A transport answering the batches in order, with hasNextPage true until the last."""

    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = batches
        self.calls: list[dict[str, Any]] = []

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(variables)
        i = len(self.calls) - 1
        return {
            "viewer": {"id": VIEWER},
            "issues": {
                "pageInfo": {
                    "hasNextPage": i < len(self.batches) - 1,
                    "endCursor": f"c{i}",
                },
                "nodes": self.batches[i],
            },
        }


def write(tmp_path: Path, post: Post, **req: Any) -> ModelLinearSnapshotResult:
    return HandlerLinearSnapshot(post).handle(
        ModelLinearSnapshotRequest(out_dir=str(tmp_path), date="2026-11-03", **req)
    )


def test_snapshot_json_shape_carries_every_field(tmp_path: Path) -> None:
    result = write(tmp_path, Pages([node(1), node(2, description=None)]))
    assert (result.status, result.issue_count, result.with_description) == (
        "written",
        2,
        1,
    )
    path = tmp_path / "linear-2026-11-03.json"
    assert result.path == str(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["operator"] == VIEWER
    first = doc["issues"][0]
    assert first == {
        "id": "OMN-1",
        "title": "Ticket 1",
        "status": "In Progress",
        "statusType": "started",
        "assignee": VIEWER,
        "labels": ["delegation", "machinery"],
        "project": "Delegation Pillar",
        "projectId": "",
        "parentId": None,
        "updatedAt": "2026-11-03T14:00:00.000Z",
        "description": "Body of the ticket.",
        "priority": "High",
        "attachments": None,  # the answer carried no parts: not read, never "has none"
        "children": None,
    }
    assert doc["issues"][1]["description"] == ""
    # the typed model reads the file back as written
    assert len(ModelLinearSnapshot.model_validate(doc).issues) == 2


def test_snapshot_json_shape_is_read_by_the_plans_today_loaders(tmp_path: Path) -> None:
    write(tmp_path, Pages([node(1), node(2, priority=0)]))
    path = tmp_path / "linear-2026-11-03.json"
    sidecar = ModelLinearSidecar.model_validate(json.loads(path.read_text()))
    assert sidecar.subject == VIEWER
    assert [i.assignee for i in sidecar.issues] == [VIEWER, VIEWER]
    linear = load_linear(path)
    assert linear["OMN-1"].status_type == "started"
    assert linear["OMN-1"].priority == "high"
    assert linear["OMN-2"].priority is None


def test_snapshot_json_shape_refuses_me_and_foreign_assignees() -> None:
    issue = {
        "id": "OMN-1",
        "title": "t",
        "status": "Todo",
        "statusType": "unstarted",
        "assignee": "me",
        "updatedAt": "2026-11-03T00:00:00Z",
    }
    with pytest.raises(ValidationError, match="literal 'me'"):
        ModelLinearSnapshot.model_validate({"operator": "me", "issues": [issue]})
    with pytest.raises(ValidationError, match="not assigned to the operator"):
        ModelLinearSnapshot.model_validate({"operator": VIEWER, "issues": [issue]})
    with pytest.raises(ValidationError):
        ModelLinearSnapshot.model_validate(
            {"operator": VIEWER, "issues": [{**issue, "assignee": VIEWER, "id": "x"}]}
        )


def test_snapshot_json_shape_follows_every_page(tmp_path: Path) -> None:
    post = Pages([node(1), node(2)], [node(3)])
    result = write(tmp_path, post, page_size=2)
    assert result.issue_count == 3
    assert post.calls == [{"first": 2, "after": None}, {"first": 2, "after": "c0"}]


def test_snapshot_json_shape_never_writes_a_cut_short_list(tmp_path: Path) -> None:
    result = write(tmp_path, Pages([node(1)], [node(2)]), max_pages=1)
    assert result.status == "failed"
    assert "next page" in result.reason
    assert not (tmp_path / "linear-2026-11-03.json").exists()


def test_snapshot_json_shape_keeps_yesterdays_file_when_the_read_fails(
    tmp_path: Path,
) -> None:
    target = tmp_path / "linear-2026-11-03.json"
    target.write_text("{}", encoding="utf-8")

    def boom(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise SnapshotReadError("LINEAR_API_KEY is not set")

    result = write(tmp_path, boom)
    assert result.status == "failed"
    assert "LINEAR_API_KEY" in result.reason
    assert target.read_text(encoding="utf-8") == "{}"


def test_snapshot_json_shape_cli_prints_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert main(["--out-dir", str(tmp_path), "--date", "2026-11-03"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("linear-snapshot | failed ")
    assert "LINEAR_API_KEY" in out


def test_snapshot_json_shape_node_is_registered() -> None:
    root = Path(__file__).resolve().parents[3]
    entries = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert entries["node_linear_snapshot_effect"] == (
        "omnimarket.nodes.node_linear_snapshot_effect"
    )
