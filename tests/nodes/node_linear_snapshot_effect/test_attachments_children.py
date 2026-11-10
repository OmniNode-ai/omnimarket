# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each issue of the Linear snapshot holds its attachments and its child ids.

The brief check's merged-implementation rule (an issue with no children and a merged implementation pull request
attached reads ``merged``) needs both parts. The snapshot lacked them, so every check read each ticket from Linear
and, when Linear answered HTTP 429, a dispatch was lost. ``None`` in the file means a part was not read; a reader that
needs it reads Linear. ``()`` means read and empty. The controls below each name the break they fail under.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot as handler_module,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    QUERY,
    QUERY_CHANGES,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
)

from .test_rate_limit_backoff import (
    T0,
    VIEWER,
    Clock,
    Script,
    issues_in,
    node,
    page,
    run,
)

PR = {
    "sourceType": "github",
    "title": "fix(OMN-22866): the check reads the mirror (#1763)",
    "metadata": {"status": "merged", "number": 1763},
}


def parts(
    attachments: list[dict[str, Any]], children: list[str], *, more: str = ""
) -> dict[str, Any]:
    """The two parts of a node as Linear answers them; ``more`` names a part whose page has a next page."""
    return {
        "attachments": {
            "pageInfo": {"hasNextPage": more == "attachments"},
            "nodes": attachments,
        },
        "children": {
            "pageInfo": {"hasNextPage": more == "children"},
            "nodes": [{"identifier": c} for c in children],
        },
    }


def write_legacy_base(out_dir: Path) -> None:
    """A snapshot and sidecar as the node wrote them before it held the parts: no ``attachments``, no ``children``."""
    doc = {
        "operator": VIEWER,
        "ancestors": [],
        "issues": [
            {
                "id": f"OMN-{n}",
                "title": f"Ticket {n}",
                "status": "In Progress",
                "statusType": "started",
                "assignee": VIEWER,
                "labels": [],
                "project": "",
                "updatedAt": "2026-11-08T06:00:00.000Z",
                "description": f"Body {n}",
                "priority": None,
            }
            for n in (1, 2)
        ],
    }
    (out_dir / "linear-2026-11-08.json").write_text(json.dumps(doc), encoding="utf-8")
    stamp = T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    (out_dir / "linear-snapshot-state.json").write_text(
        json.dumps(
            {
                "schema": "omni.linear-snapshot-state/1",
                "last_good_read_at": stamp,
                "last_full_read_at": stamp,
                "last_attempt_at": stamp,
                "last_attempt_status": "written",
                "last_attempt_reason": "",
                "age_s": 0,
            }
        ),
        encoding="utf-8",
    )


def test_a_full_read_writes_each_issues_attachments_and_child_ids(
    tmp_path: Path,
) -> None:
    post = Script(
        page(
            [
                node(1, **parts([PR], [])),
                node(2, **parts([], ["OMN-21", "OMN-22"])),
            ]
        )
    )
    result = run(tmp_path, post, Clock())
    assert (result.status, result.mode, result.issue_count) == ("written", "full", 2)
    assert "attachments(first: 50)" in post.calls[0][0]
    assert "children(first: 50)" in post.calls[0][0]
    docs = issues_in(tmp_path)
    assert docs["OMN-1"]["attachments"] == [
        {"sourceType": "github", "title": PR["title"], "metadata": PR["metadata"]}
    ]
    assert docs["OMN-1"]["children"] == []
    assert docs["OMN-2"]["attachments"] == []
    assert docs["OMN-2"]["children"] == ["OMN-21", "OMN-22"]
    reread = ModelLinearSnapshot.model_validate_json(
        (tmp_path / "linear-2026-11-08.json").read_text(encoding="utf-8")
    )
    assert reread.issues[0].attachments is not None


def test_the_incremental_read_selects_the_parts_and_replaces_a_changed_issues_parts(
    tmp_path: Path,
) -> None:
    run(
        tmp_path,
        Script(page([node(1, **parts([], [])), node(2, **parts([PR], []))])),
        Clock(),
    )
    changed = node(1, "2026-11-08T07:30:00.000Z", **parts([PR], ["OMN-31"]))
    post = Script(page([changed]))
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=1)))
    assert result.mode == "incremental"
    assert "attachments(first: 50)" in post.calls[0][0]
    assert "children(first: 50)" in post.calls[0][0]
    docs = issues_in(tmp_path)
    assert docs["OMN-1"]["children"] == ["OMN-31"]
    assert docs["OMN-1"]["attachments"][0]["title"] == PR["title"]
    assert (
        docs["OMN-2"]["attachments"][0]["title"] == PR["title"]
    )  # untouched, kept as read


def test_both_queries_select_the_same_parts() -> None:
    for query in (QUERY, QUERY_CHANGES):
        assert (
            "attachments(first: 50) { pageInfo { hasNextPage } nodes { sourceType title metadata } }"
            in query
        )
        assert (
            "children(first: 50) { pageInfo { hasNextPage } nodes { identifier } }"
            in query
        )


@pytest.mark.parametrize("more", ["attachments", "children"])
def test_a_part_with_a_next_page_is_written_as_not_read_never_as_cut_short(
    tmp_path: Path, more: str
) -> None:
    run(
        tmp_path, Script(page([node(1, **parts([PR], ["OMN-21"], more=more))])), Clock()
    )
    doc = issues_in(tmp_path)["OMN-1"]
    other = "children" if more == "attachments" else "attachments"
    assert doc[more] is None
    assert doc[other] is not None


def test_a_snapshot_written_before_the_parts_existed_is_read_in_full_not_incrementally(
    tmp_path: Path,
) -> None:
    write_legacy_base(tmp_path)
    post = Script(page([node(1, **parts([PR], [])), node(2, **parts([], []))]))
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=1)))
    assert (result.status, result.mode) == ("written", "full")
    assert post.calls[0][0] == QUERY
    docs = issues_in(tmp_path)
    assert docs["OMN-1"]["attachments"]
    assert docs["OMN-2"]["attachments"] == []


def test_control_without_the_legacy_check_an_old_base_stays_without_its_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named break: ``_lacks_parts`` always False. The read is incremental and the untouched issue keeps no parts,
    which is the state ``test_a_snapshot_written_before_the_parts_existed...`` refuses."""
    monkeypatch.setattr(handler_module, "_lacks_parts", lambda _snapshot: False)
    write_legacy_base(tmp_path)
    post = Script(page([node(1, "2026-11-08T07:30:00.000Z", **parts([PR], []))]))
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=1)))
    assert result.mode == "incremental"
    docs = issues_in(tmp_path)
    assert docs["OMN-2"]["attachments"] is None
    assert docs["OMN-2"]["children"] is None


def test_a_node_without_the_parts_in_the_answer_is_written_as_not_read(
    tmp_path: Path,
) -> None:
    bare = node(1)
    bare.pop("attachments")
    bare.pop("children")
    run(tmp_path, Script(page([bare])), Clock())
    doc = issues_in(tmp_path)["OMN-1"]
    assert doc["attachments"] is None
    assert doc["children"] is None
