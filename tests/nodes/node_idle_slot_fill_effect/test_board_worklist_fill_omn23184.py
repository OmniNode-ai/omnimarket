# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill takes its candidates from the board's work list first (OMN-23184).

The operator, 2026-11-08 about 23:00Z: the board has a list of everything that is stopped and everything that is red and needs
help, so a free slot should take from it. The floor's published document carries one row per work class with every member and
its owner state; the fill takes the unowned members first, in the board's order, and takes nothing a lane or lease owns.
The failing control is the same input with the overlay's ``board`` block taken away: the fill sends the stored backlog item
and not the board's.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a.model_worklist import ModelWorklistItemIn
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelLabFillRequest
from tests.nodes.node_floor_simple_effect.test_worklist_floor_simple import (
    doc_for,
    item,
    lane,
)
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
    IDLE_SLOT_FILL,
    write_overlay,
)

NOW = "2026-11-08T23:30:00Z"
BOARD = {
    "floor_relpath": "floor/floor-simple.json",
    "max_age_s": 900,
    "classes": ["pr", "stopped", "shared_red"],
}


def floor_document(
    items: tuple[ModelWorklistItemIn, ...], *, age_s: int = 60
) -> dict[str, Any]:
    doc = json.loads(doc_for(items).model_dump_json())
    made = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=age_s)
    doc["generated_at"] = made.strftime("%Y-%m-%dT%H:%M:%SZ")
    return doc


def setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, Any] | None,
    *,
    board: dict[str, Any] | None = BOARD,
) -> ModelLabFillRequest:
    overlay = {**IDLE_SLOT_FILL, **({"board": board} if board is not None else {})}
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        write_overlay(tmp_path / "overlays", idle_slot_fill=overlay)[
            "ONEX_SKILL_OVERLAY_ROOTS"
        ],
    )
    registry = tmp_path / "registry"
    monkeypatch.setenv("ONEX_REGISTRY_ROOT", str(registry))
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    if document is not None:
        path = registry / "floor/floor-simple.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "standing_tickets": {},
                "items": [
                    {
                        "kind": "m4-board-red",
                        "key": "stored-red",
                        "repo": "omnimarket",
                        "ticket": "OMN-21396",
                    }
                ],
            }
        )
    )
    return ModelLabFillRequest(
        now=NOW,
        request={
            "lab_headroom": {
                "hosts": [{"name": "h201", "admission": {"claude_slots": 1}}]
            }
        },
        lines=["MISSING lab-headroom:h201 | free Claude slots 1"],
        launch=None,
        state_dir=tmp_path / "state",
        backlog=str(backlog),
        runner=tmp_path / "runner.py",
        out_of_scope={},
        lanes_dir=tmp_path / "lanes",
    )


def run(
    request: ModelLabFillRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[list[str]], Any]:
    sent: list[list[str]] = []
    monkeypatch.setattr(fill, "_send", lambda argv: (sent.append(argv) is None, "sent"))
    return sent, fill.HandlerLabFill().handle(request)


def arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


UNOWNED = (
    item("omnimarket#101", ticket="OMN-23184", title="a red PR", age_s=7200),
    item("omnimarket#102", ticket="OMN-23184", title="a young red PR", age_s=60),
)


def test_golden_the_fill_sends_the_oldest_unowned_board_item_before_the_stored_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = setup(tmp_path, monkeypatch, floor_document(UNOWNED))
    sent, _ = run(request, monkeypatch)
    assert len(sent) == 1
    assert arg(sent[0], "--repo") == "omnimarket"
    lane_name = arg(sent[0], "--lane")
    assert lane_name.startswith("fill-board-pr-omnimarket-101-")
    assert arg(sent[0], "--ticket") == "OMN-23184"


def test_error_control_without_the_overlays_board_block_the_stored_item_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = setup(tmp_path, monkeypatch, floor_document(UNOWNED), board=None)
    sent, _ = run(request, monkeypatch)
    assert len(sent) == 1
    assert arg(sent[0], "--lane").startswith("fill-m4-")


def test_golden_an_item_a_lane_or_a_lease_owns_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = (
        item("omnimarket#101", ticket="OMN-23184", age_s=7200),
        item("omnimarket#102", ticket="OMN-23184", landing_lease="landing-L4"),
        item("omnimarket#103", ticket="OMN-23184", cause_lease="cause-L5 k"),
    )
    doc = floor_document(items)
    # a live lane on #101 as well: every item now has an owner
    doc_owned = json.loads(
        doc_for(items, (lane("l1", pr="omnimarket#101"),)).model_dump_json()
    )
    doc_owned["generated_at"] = doc["generated_at"]
    request = setup(tmp_path, monkeypatch, doc_owned)
    sent, _ = run(request, monkeypatch)
    assert len(sent) == 1
    assert arg(sent[0], "--lane").startswith("fill-m4-")


def test_error_a_stale_board_is_reported_and_the_fill_goes_on_to_its_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = setup(tmp_path, monkeypatch, floor_document(UNOWNED, age_s=3600))
    sent, result = run(request, monkeypatch)
    assert arg(sent[0], "--lane").startswith("fill-m4-")
    note = next(row for row in result.found if row[1] == "lab-fill-board")
    assert note[0] == "unknown"
    assert "3600 s old" in note[2]


def test_error_an_absent_board_is_reported_not_read_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = setup(tmp_path, monkeypatch, None)
    sent, result = run(request, monkeypatch)
    assert arg(sent[0], "--lane").startswith("fill-m4-")
    note = next(row for row in result.found if row[1] == "lab-fill-board")
    assert "is unread" in note[2]


def test_board_items_take_the_declared_classes_only() -> None:
    items = (
        item("omnimarket#1", ticket="OMN-1"),
        item("OMN-9", kind="stopped", ticket="OMN-9", prs=(), title="stopped"),
        item(
            "omnibase_infra:CI Summary",
            kind="shared_red",
            prs=("omnibase_infra#1", "omnibase_infra#2"),
        ),
    )
    document = floor_document(items)
    now = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ")
    every, why = fill.board_items(
        document, now=now, max_age_s=900, classes=["pr", "stopped", "shared_red"]
    )
    assert why == ""
    assert [(i["kind"], i["key"], i["repo"]) for i in every] == [
        ("board-pr", "omnimarket#1", "omnimarket"),
        ("board-stopped", "board-stopped:OMN-9", ""),
        (
            "board-shared-red",
            "board-shared-red:omnibase_infra:CI Summary",
            "omnibase_infra",
        ),
    ]
    only, _ = fill.board_items(document, now=now, max_age_s=900, classes=["stopped"])
    assert [i["kind"] for i in only] == ["board-stopped"]
    assert only[0]["ticket"] == "OMN-9"


def test_board_candidates_lead_and_replace_a_process_fix_line_for_the_same_pull_request() -> (
    None
):
    board = [
        {
            "kind": "board-pr",
            "key": "omnimarket#7",
            "repo": "omnimarket",
            "name": "omnimarket-7",
            "fact": "board pr omnimarket#7",
        }
    ]
    lines = [
        "MISSING process-fix:omnimarket#7 | unowned",
        "MISSING repo-status:omnibase_infra | shared_red_unowned: pytest",
    ]
    ordered = fill.candidates(lines, [], (), board=board)
    assert [(c["kind"], c["key"]) for c in ordered] == [
        ("board-pr", "omnimarket#7"),
        ("cause-red", "repo-status:omnibase_infra"),
    ]
