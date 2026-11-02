# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the two recorded cause-ticket-unresolved rows through the detector and the actor (OMN-22143).

The queue in a temporary directory is built as recorded: the 2026-10-30 row was appended and later resolved, and
the 2026-11-02 row, the same key, is open. With the lane_dispatch overlay naming the open standing ticket
OMN-22143 for merge-sweep-diagnose, both rows resolve in code and no row of the class stays open. The control,
an overlay with no standing ticket, leaves the case open and emits no new row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_cause_ticket_unresolved_effect.handler import (
    CLASS,
    HandlerCauseTicketUnresolved,
    classify_block,
    read_standing_tickets,
)
from omnimarket.nodes.node_cause_ticket_unresolved_effect.models import (
    ModelCauseTicketUnresolvedRequest,
)
from omnimarket.nodes.node_cause_ticket_unresolved_effect.node import (
    NodeCauseTicketUnresolvedEffect,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_resolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent / "fixtures" / "cause_ticket_unresolved_recorded_rows.json"
)
RECORDED: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
NOW = "2026-11-02T16:00:00Z"
STANDING = {"merge-sweep-diagnose": "OMN-22143"}
STATES = {"OMN-22143": "Backlog"}


def _emit(state_dir: Path, row: dict[str, Any]) -> str:
    result = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=state_dir / "exception-queue",
            row=ModelExceptionRow(
                cls=row["cls"],
                key=row["key"],
                source=row["source"],
                evidence=row["evidence"],
                tried=row["tried"],
                proposed_fix=row["proposed_fix"],
                decision=bool(row.get("decision")),
            ),
            now=row["at"],
        )
    )
    return result.id


def recorded_queue(state_dir: Path) -> None:
    """The queue as recorded: the first row appended and resolved, the second appended and open."""
    (state_dir / "exception-queue").mkdir(parents=True)
    first, second = RECORDED
    assert _emit(state_dir, first) == first["id"]
    create_handler_exception_resolve().handle(
        ModelExceptionResolveRequest(
            state_dir / "exception-queue",
            first["id"],
            "orchestrator",
            "resolved by hand",
            "2026-10-30T14:00:00Z",
        )
    )
    assert _emit(state_dir, second) == second["id"]


def open_rows(state_dir: Path) -> list[dict[str, Any]]:
    return (
        create_handler_exception_queue()
        .handle(
            ModelExceptionQueueRequest(state_dir / "exception-queue", "orchestrator")
        )
        .rows
    )


def settle(
    state_dir: Path,
    rows: list[dict[str, Any]],
    *,
    standing: dict[str, str] | None = None,
    states: dict[str, str] | None = None,
) -> Any:
    return HandlerCauseTicketUnresolved().handle(
        ModelCauseTicketUnresolvedRequest(
            state_dir=state_dir,
            rows=tuple(rows),
            standing_tickets=STANDING if standing is None else standing,
            ticket_states=STATES if states is None else states,
            now=NOW,
        )
    )


def test_detector_reads_both_recorded_rows_as_floor_only_diagnose() -> None:
    for row in RECORDED:
        case = classify_block(row)
        assert case is not None
        assert case.id == row["id"]
        assert case.lane_kind == "diagnose"
        assert case.overlay_key == "merge-sweep-diagnose"
        assert case.reasons == ("under floor: omnimarket",)
        assert case.reds == ()
        assert case.repo is None
        assert case.floor_only is True


def test_detector_ignores_other_blocks() -> None:
    other = dict(RECORDED[0], tried="retried the durable health read; it still fails")
    assert classify_block(other) is None
    assert (
        classify_block(dict(RECORDED[0], key="bus-binding lane=x state=degraded"))
        is None
    )
    red = dict(
        RECORDED[0],
        key='merge-sweep-fix:{"reasons": [], "reds": ["OmniNode-ai/omnimarket#1"], "repo": "omnimarket"}',
    )
    case = classify_block(red)
    assert case is not None
    assert case.floor_only is False
    assert case.overlay_key == "merge-sweep-fix"


def test_replay_resolves_both_recorded_rows_under_the_standing_ticket(
    tmp_path: Path,
) -> None:
    recorded_queue(tmp_path)
    assert [r["id"] for r in open_rows(tmp_path)] == [RECORDED[1]["id"]]

    result = settle(tmp_path, RECORDED)

    assert [r.resolved for r in result.resolutions] == [True, True]
    assert {r.ticket for r in result.resolutions} == {"OMN-22143"}
    assert all(r.emitted == "" for r in result.resolutions)
    assert [r for r in open_rows(tmp_path) if r["cls"] == CLASS] == []


def test_control_with_no_standing_ticket_stays_open(tmp_path: Path) -> None:
    recorded_queue(tmp_path)

    result = settle(tmp_path, RECORDED, standing={}, states={})

    assert [r.resolved for r in result.resolutions] == [False, False]
    assert all(r.emitted == f"repeat:{RECORDED[1]['id']}" for r in result.resolutions)
    assert "no standing_tickets['merge-sweep-diagnose']" in result.resolutions[0].how
    assert [r["id"] for r in open_rows(tmp_path)] == [RECORDED[1]["id"]]


@pytest.mark.parametrize(
    ("standing", "states", "why"),
    [
        ({"merge-sweep-diagnose": "OMN-17427"}, {"OMN-17427": "Backlog"}, "umbrella"),
        ({"merge-sweep-diagnose": "OMN-22143"}, {"OMN-22143": "Done"}, "Done"),
        ({"merge-sweep-diagnose": "OMN-22143"}, {}, "unread"),
        ({"merge-sweep-diagnose": "later"}, {}, "not a ticket id"),
    ],
)
def test_unusable_standing_ticket_is_emitted_not_resolved(
    tmp_path: Path, standing: dict[str, str], states: dict[str, str], why: str
) -> None:
    result = settle(tmp_path, [RECORDED[1]], standing=standing, states=states)

    (resolution,) = result.resolutions
    assert resolution.resolved is False
    assert why in resolution.how
    assert resolution.emitted.startswith("appended:")
    (row,) = open_rows(tmp_path)
    assert row["cls"] == CLASS
    assert row["key"] == RECORDED[1]["key"]
    assert row["tried"] == resolution.how


def test_decision_row_is_never_resolved(tmp_path: Path) -> None:
    result = settle(tmp_path, [dict(RECORDED[1], decision=True)])

    (resolution,) = result.resolutions
    assert resolution.resolved is False
    assert resolution.how == "decision row: left to the operator"
    assert resolution.emitted == ""
    assert not (tmp_path / "exception-queue").exists()


def test_overlay_standing_tickets_read_from_the_overlay_file(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "name: lane_dispatch\nstanding_tickets:\n  merge-sweep-diagnose: OMN-22143\n",
        encoding="utf-8",
    )
    assert read_standing_tickets(overlay) == STANDING
    assert read_standing_tickets(tmp_path / "absent.yaml") == {}
    (tmp_path / "bare.yaml").write_text("name: lane_dispatch\n", encoding="utf-8")
    assert read_standing_tickets(tmp_path / "bare.yaml") == {}


def test_node_entrypoint_runs_the_handler(tmp_path: Path) -> None:
    recorded_queue(tmp_path)
    result = NodeCauseTicketUnresolvedEffect().handle(
        ModelCauseTicketUnresolvedRequest(
            state_dir=tmp_path,
            rows=(RECORDED[1],),
            standing_tickets=STANDING,
            ticket_states=STATES,
            now=NOW,
        )
    )
    assert [r.resolved for r in result.resolutions] == [True]
    assert open_rows(tmp_path) == []
