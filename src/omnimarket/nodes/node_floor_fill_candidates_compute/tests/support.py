# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the floor-fill candidates tests: the checked-in record, small records and requests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillStateEntry,
    ModelFloorTaskPayloadVerdict,
)

FIXTURE = Path(__file__).parent / "fixtures" / "factory-floor-2026-11-02T1040Z.json"
NOW = "2026-11-02T10:45:00Z"
MAX_AGE = 30
MAX_LANES = 2
WINDOW = 60
STOPPED_LANES = 2
REPORT_CHARS = 600
RULE = "Push the predecessor's branch and continue from its head; do not start over and do not wait on a reply."

# The ready lane tasks of the 10:40Z record, in record order, per line: the pinned table of the test.
READY_10_40 = {
    "2026-10-01-line-a-plan": ("T0.1", "T0.2", "T0.3"),
    "2026-10-02-line-b-plan": ("CM.0", "CM.1", "CM.8"),
    "2026-10-03-line-c-plan": ("T0.1", "T0.2", "T1.1"),
    "2026-10-04-line-d-plan": ("T7", "T8", "T10"),
    "2026-10-05-line-e-plan": ("W0.1", "W0.2", "W0.3"),
    "2026-10-06-line-f-plan": ("S1", "S3", "S4"),
}
LINE = "2026-11-02-line-x-plan"


def fixture_text() -> str:
    return FIXTURE.read_text()


def key(line_id: str, task_id: str) -> str:
    return f"floor:{line_id}:{task_id}"


def item(
    task_id: str,
    state: str = "prep",
    note: str = "ready",
    *,
    kind: str = "lane",
    prs: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "kind": kind,
        "state": state,
        "note": note,
        "tickets": [],
        "prs": list(prs),
    }


def record(
    items: list[dict[str, Any]],
    *,
    generated_at: str = "2026-11-02T10:40:00Z",
    pill: str = "stopped",
    lanes: list[dict[str, Any]] | None = None,
    line_id: str = LINE,
) -> str:
    return json.dumps(
        {
            "schema_id": "omni.factory-floor/1",
            "generated_at": generated_at,
            "lines": [
                {
                    "line_id": line_id,
                    "name": "Line X",
                    "status_pill": pill,
                    "stations": [{"id": "tasks", "state": "active", "items": items}],
                }
            ],
            "floor": {"lanes": lanes or []},
        }
    )


def minutes_before(now: str, minutes: int) -> str:
    at = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(minutes=minutes)
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def usable(line_id: str, task_id: str, ticket: str) -> ModelFloorEntryTicketVerdict:
    return ModelFloorEntryTicketVerdict(
        line_id=line_id, task_id=task_id, status="usable", ticket=ticket
    )


def payload_ok(line_id: str, task_id: str) -> ModelFloorTaskPayloadVerdict:
    return ModelFloorTaskPayloadVerdict(line_id=line_id, task_id=task_id, ok=True)


def reserved(line_id: str, task_id: str) -> ModelFloorFillStateEntry:
    return ModelFloorFillStateEntry(
        key=key(line_id, task_id), status="RESERVED", at=NOW
    )


def sent(
    line_id: str, task_id: str, *, at: str = NOW, ended: bool = False
) -> ModelFloorFillStateEntry:
    return ModelFloorFillStateEntry(
        key=key(line_id, task_id), status="SENT", at=at, ended=ended
    )


def request(**overrides: Any) -> ModelFloorFillCandidatesRequest:
    fields: dict[str, Any] = {
        "now": NOW,
        "floor_record": fixture_text(),
        "max_age_minutes": MAX_AGE,
        "max_fill_lanes_per_line": MAX_LANES,
        "sent_window_minutes": WINDOW,
        "stopped_max_lanes": STOPPED_LANES,
        "stopped_report_chars": REPORT_CHARS,
        "stopped_continue_rule": RULE,
    }
    fields.update(overrides)
    return ModelFloorFillCandidatesRequest(**fields)


def all_ready_tickets_usable() -> tuple[ModelFloorEntryTicketVerdict, ...]:
    return tuple(
        usable(line, task, f"TCK-{n}")
        for n, (line, task) in enumerate(
            (line, task) for line, tasks in READY_10_40.items() for task in tasks
        )
    )


def all_ready_payloads_ok() -> tuple[ModelFloorTaskPayloadVerdict, ...]:
    return tuple(
        payload_ok(line, task) for line, tasks in READY_10_40.items() for task in tasks
    )
