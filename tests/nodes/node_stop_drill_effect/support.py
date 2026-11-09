# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the piece 7 reading and stop drill tests (OMN-22943): an overlay, a clock, a recording ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_process_spine_reading_compute.models import (
    EnumDrillSend,
    ModelDrillObservation,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay

NOW = datetime(2026, 11, 9, 9, 20, tzinfo=UTC)
MAX_AGE = 7200
LANE = "test-stop-drill"


def overlay(**changes: object) -> ModelStopDrillOverlay:
    document: dict[str, object] = {
        "overlay_revision": "test.1",
        "drill_piece": "drill_piece",
        "control_piece": "control_piece",
        "store_limits": {"drill_piece": 3, "control_piece": 3},
        "reservation_ttl_seconds": 60,
        "max_age_seconds": MAX_AGE,
        "replay_required": False,
        "emission": {
            "home_env": "TEST_HOME",
            "journal_relpath": ".onex_state/floor-simple/emissions.jsonl",
            "piece_number": 7,
            "chain_node": "node_stop_drill_effect:run",
            "ledger": {
                "lane": LANE,
                "actor": "node:node_stop_drill_effect",
                "command_env": "TEST_APPEND_COMMAND",
                "host_env": "TEST_HOST",
                "timeout_seconds": 30,
            },
        },
        "schedule_hosts": [{"name": "h053", "venv": "venv"}],
        "unit_environment": {"TEST_HOST": "h053", "TEST_HOME": "$OMNI_HOME"},
    }
    document.update(changes)
    return ModelStopDrillOverlay.model_validate(document)


def observation(
    *,
    at: datetime = NOW,
    stop_committed: bool = True,
    drill_send: EnumDrillSend = EnumDrillSend.REFUSED,
    control_send: EnumDrillSend = EnumDrillSend.ADMITTED,
) -> ModelDrillObservation:
    return ModelDrillObservation(
        drill_id="drill-1",
        probed_at=at,
        drill_lane="drill_piece",
        control_lane="control_piece",
        stop_committed=stop_committed,
        drill_send=drill_send,
        control_send=control_send,
    )


def minutes_ago(minutes: int) -> datetime:
    return NOW - timedelta(minutes=minutes)


class RecordingAppender:
    """The bus append command, in memory: the rows it was given, each under its request id."""

    def __init__(self, status: str = "accepted") -> None:
        self.ledger = InMemoryLedger()
        self.ledger.append_status = status
        self.rows: list[str] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        outcome = self.ledger.append(rows, request_id)
        if outcome.status == "accepted":
            self.rows.extend(line for line in rows.splitlines() if line.strip())
        return outcome
