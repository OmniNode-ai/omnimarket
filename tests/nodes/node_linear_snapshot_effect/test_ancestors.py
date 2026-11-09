# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear snapshot carries the ancestors of its issues, so a chain through an unassigned or closed ticket still reads (OMN-23184).

The snapshot lists only the operator's open assigned issues; the chain OMN-23229 -> OMN-23127 -> OMN-22880 -> OMN-22478
broke where OMN-23127 and OMN-22880 are in nobody's open list. The query nests `parent { ... }` to `ancestor_depth`
levels, the file gets a top-level `ancestors` list (every ancestor that is not itself an issue, once), and the
incremental read keeps it consistent with the merged issues.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot as handler,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelLinearSnapshotRequest,
)
from tests.nodes.node_linear_snapshot_effect.test_rate_limit_backoff import (
    T0,
    Clock,
    Script,
    node,
    page,
    run,
)
from tests.nodes.node_linear_snapshot_effect.test_snapshot_json_shape import (
    VIEWER,
)

pytestmark = pytest.mark.unit

FACTORY_ID = "ae87f221-ad60-49fe-aeae-b4417960ff90"


def chain(*idents: int, project: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The nested `parent` object of an issue whose ancestors are `idents`, nearest first."""
    if not idents:
        return None
    head, *rest = idents
    out: dict[str, Any] = {"identifier": f"OMN-{head}", "project": project}
    out["parent"] = chain(*rest, project=project)
    return out


def doc(tmp_path: Path, date: str = "2026-11-08") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (tmp_path / f"linear-{date}.json").read_text(encoding="utf-8")
    )
    return loaded


def ancestors_in(tmp_path: Path) -> dict[str, dict[str, Any]]:
    return {a["id"]: a for a in doc(tmp_path)["ancestors"]}


def test_the_query_nests_parent_to_the_ancestor_depth() -> None:
    for build in (handler.build_query, handler.build_changes_query):
        deep = build(3)
        # three levels name their project; the one past them names only the identifier, the third level's parentId
        assert deep.count("parent {") == 3 + 1
        assert "parent { identifier project { id name } parent {" in deep
        assert deep.count("project { id name }") == 1 + 3
        # depth 0 asks the parent identifier only, as before
        assert build(0).count("parent {") == 1
        assert build(0).count("project { id name }") == 1


def test_the_request_depth_defaults_to_six_and_is_bounded() -> None:
    assert ModelLinearSnapshotRequest(out_dir="x").ancestor_depth == 6
    for bad in (-1, 9):
        with pytest.raises(ValidationError):
            ModelLinearSnapshotRequest(out_dir="x", ancestor_depth=bad)
    ModelLinearSnapshotRequest(out_dir="x", ancestor_depth=0)
    ModelLinearSnapshotRequest(out_dir="x", ancestor_depth=8)


def test_the_post_receives_a_query_nested_to_the_request_depth(tmp_path: Path) -> None:
    post = Script(page([node(1)]))
    run(tmp_path, post, Clock(), ancestor_depth=2)
    assert post.calls[0][0].count("parent {") == 2 + 1


def test_the_file_lists_each_ancestor_not_in_issues_once(tmp_path: Path) -> None:
    f = {"id": FACTORY_ID, "name": "Factory"}
    issues = [
        node(
            23229,
            parent={
                "identifier": "OMN-23127",
                "project": None,
                "parent": {
                    "identifier": "OMN-22880",
                    "project": f,
                    "parent": {"identifier": "OMN-22478", "project": None},
                },
            },
        ),
        # a sibling under the same ancestors, and one issue that is itself an ancestor of another
        node(
            23230,
            parent={
                "identifier": "OMN-23127",
                "project": None,
                "parent": {
                    "identifier": "OMN-22880",
                    "project": f,
                    "parent": {"identifier": "OMN-22478", "project": None},
                },
            },
        ),
        node(22478),
    ]
    run(tmp_path, Script(page(issues)), Clock())
    rows = ancestors_in(tmp_path)
    assert set(rows) == {"OMN-23127", "OMN-22880"}  # OMN-22478 is an issue
    assert len(doc(tmp_path)["ancestors"]) == 2
    assert rows["OMN-23127"] == {
        "id": "OMN-23127",
        "parentId": "OMN-22880",
        "project": "",
        "projectId": "",
    }
    assert rows["OMN-22880"] == {
        "id": "OMN-22880",
        "parentId": "OMN-22478",
        "project": "Factory",
        "projectId": FACTORY_ID,
    }
    assert [i["id"] for i in doc(tmp_path)["issues"]].count("OMN-23127") == 0


def test_an_old_file_without_the_key_still_reads() -> None:
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
    assert ModelLinearSnapshot.model_validate(old).ancestors == ()
    with_key = ModelLinearSnapshot.model_validate(
        {
            **old,
            "ancestors": [
                {"id": "OMN-9", "parentId": None, "project": "", "projectId": ""}
            ],
        }
    )
    assert with_key.ancestors[0].id == "OMN-9"
    with pytest.raises(ValidationError):
        ModelLinearSnapshot.model_validate(
            {**old, "ancestors": [{"id": "OMN-9", "surprise": 1}]}
        )


def _seed_with_chain(tmp_path: Path) -> Clock:
    clock = Clock(T0)
    deep = chain(23127, 22880, 22478)
    run(tmp_path, Script(page([node(23229, parent=deep), node(2)])), clock)
    return clock


def test_the_incremental_read_keeps_the_ancestors_of_unchanged_issues(
    tmp_path: Path,
) -> None:
    _seed_with_chain(tmp_path)
    assert set(ancestors_in(tmp_path)) == {"OMN-23127", "OMN-22880", "OMN-22478"}
    # an unrelated change: the other issue is renamed; OMN-23229 is not re-read
    result = run(
        tmp_path,
        Script(page([node(2, "2026-11-08T07:30:00.000Z", title="renamed")])),
        Clock(T0 + timedelta(hours=1)),
    )
    assert result.mode == "incremental"
    assert set(ancestors_in(tmp_path)) == {"OMN-23127", "OMN-22880", "OMN-22478"}


def test_the_incremental_read_refreshes_a_changed_ancestor(tmp_path: Path) -> None:
    _seed_with_chain(tmp_path)
    # the unassigned ancestor OMN-22880 moved into the Factory project: its own row is read as a non-mine node
    moved = node(
        22880,
        "2026-11-08T07:30:00.000Z",
        assignee=None,
        project={"id": FACTORY_ID, "name": "Factory"},
        parent={"identifier": "OMN-22478"},
    )
    result = run(tmp_path, Script(page([moved])), Clock(T0 + timedelta(hours=1)))
    assert result.mode == "incremental"
    rows = ancestors_in(tmp_path)
    assert set(rows) == {"OMN-23127", "OMN-22880", "OMN-22478"}
    assert rows["OMN-22880"]["projectId"] == FACTORY_ID
    assert rows["OMN-22880"]["project"] == "Factory"


def test_the_incremental_read_prunes_ancestors_no_issue_reaches(tmp_path: Path) -> None:
    _seed_with_chain(tmp_path)
    # OMN-23229 is re-parented to OMN-50; the old chain is reachable from no issue
    reparented = node(
        23229,
        "2026-11-08T07:40:00.000Z",
        parent={"identifier": "OMN-50", "project": None},
    )
    run(tmp_path, Script(page([reparented])), Clock(T0 + timedelta(hours=1)))
    rows = ancestors_in(tmp_path)
    assert set(rows) == {"OMN-50"}
    assert rows["OMN-50"]["parentId"] is None


def test_the_incremental_read_records_a_closed_issue_as_an_ancestor_of_its_children(
    tmp_path: Path,
) -> None:
    clock = Clock(T0)
    run(
        tmp_path,
        Script(
            page([node(1), node(2, parent={"identifier": "OMN-1", "project": None})])
        ),
        clock,
    )
    assert ancestors_in(tmp_path) == {}  # OMN-1 is an issue
    closed = node(
        1,
        "2026-11-08T07:50:00.000Z",
        state={"name": "Done", "type": "completed"},
        project={"id": FACTORY_ID, "name": "Factory"},
        parent={"identifier": "OMN-22478"},
    )
    result = run(tmp_path, Script(page([closed])), Clock(T0 + timedelta(hours=1)))
    assert result.mode == "incremental"
    assert [i["id"] for i in doc(tmp_path)["issues"]] == ["OMN-2"]
    assert ancestors_in(tmp_path) == {
        "OMN-1": {
            "id": "OMN-1",
            "parentId": "OMN-22478",
            "project": "Factory",
            "projectId": FACTORY_ID,
        }
    }


def test_an_issue_that_became_an_open_issue_leaves_the_ancestors(
    tmp_path: Path,
) -> None:
    _seed_with_chain(tmp_path)
    run(
        tmp_path,
        Script(page([node(22880, "2026-11-08T07:30:00.000Z")])),
        Clock(T0 + timedelta(hours=1)),
    )
    assert "OMN-22880" not in ancestors_in(tmp_path)


def test_a_base_file_from_before_the_key_is_read_in_full(tmp_path: Path) -> None:
    _seed_with_chain(tmp_path)
    path = tmp_path / "linear-2026-11-08.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    del old["ancestors"]
    path.write_text(json.dumps(old), encoding="utf-8")
    post = Script(page([node(23229, parent=chain(23127, 22880, 22478)), node(2)]))
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=1)))
    assert result.mode == "full"
    assert set(ancestors_in(tmp_path)) == {"OMN-23127", "OMN-22880", "OMN-22478"}
