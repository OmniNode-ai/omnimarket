# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the board probe-results projection (OMN-19937).

The chain is the contract's declared path: a board-probe-result event enters
the reducer, the writer persists the derived row, the accepted row leaves on
the bus-backed snapshot exposure, and the projection-applied topic remains the
declared terminal state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_board_probe_results.contract_topics import (
    TOPIC_BOARD_PROBE_RESULT,
    TOPIC_DLQ,
    TOPIC_EXPOSURE,
    TOPIC_PROJECTION_APPLIED,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.board_probe_results_fold import (
    HandlerProjectionBoardProbeResults,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.handler_board_probe_results_writer import (
    BoardProbeResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_board_probe_results.models import (
    ModelBoardProbeResultPayload,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_board_probe_results"
    / "contract.yaml"
)
_FINISHED_AT = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _payload() -> dict[str, Any]:
    return {
        "check_id": "branch-protection",
        "subject_kind": "pull_request",
        "subject": "OmniNode-ai/omnimarket#321",
        "repo": "OmniNode-ai/omnimarket",
        "sha": "a" * 40,
        "surface_instance": "github-main",
        "execution_id": "probe-run-golden-chain",
        "outcome": "PASS",
        "reasons": [],
        "evidence_items": ["probe://board/result"],
        "finished_at": _FINISHED_AT.isoformat(),
    }


def test_hop1_contract_declares_the_board_probe_input_and_its_producer() -> None:
    contract = _contract()

    assert contract["event_bus"]["subscribe_topics"] == [TOPIC_BOARD_PROBE_RESULT]
    assert contract["externally_produced_topics"] == [
        {
            "topic": TOPIC_BOARD_PROBE_RESULT,
            "producer": "omnibase_infra board probe result publisher",
        }
    ]
    assert BoardProbeResultsProjectionWriter().subscribe_topics == [
        TOPIC_BOARD_PROBE_RESULT
    ]


def test_hop2_the_definition_b_handler_folds_the_event_into_one_row() -> None:
    result = HandlerProjectionBoardProbeResults().handle(
        ModelBoardProbeResultPayload.model_validate(_payload())
    )

    assert result.row.execution_id == "probe-run-golden-chain"
    assert result.row.outcome.value == "PASS"
    assert result.row.finished_at == _FINISHED_AT


class _RecordingDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [
            {
                "projection_cursor": 1,
                "check_id": args[0],
                "subject_kind": args[1],
                "subject": args[2],
                "repo": args[3],
                "sha": args[4],
                "surface_instance": args[5],
                "execution_id": args[6],
                "outcome": args[7],
                "reasons": args[8],
                "evidence_items": args[9],
                "finished_at": args[10],
                "source_offset": args[11],
                "projected_at": args[12],
            }
        ]


@pytest.mark.asyncio
async def test_hop3_the_writer_upserts_and_publishes_the_accepted_snapshot() -> None:
    writer = BoardProbeResultsProjectionWriter()
    database = _RecordingDb()
    writer._db = database  # type: ignore[assignment]
    published: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        published.append({"topic": exposure.topic, **kwargs})
        return True

    writer.publish_snapshot_delta = _capture  # type: ignore[method-assign]
    meta = MessageMeta(
        partition=0,
        offset=41,
        fallback_id="board-probe-golden-chain",
        topic=TOPIC_BOARD_PROBE_RESULT,
    )

    assert await writer.project_event(TOPIC_BOARD_PROBE_RESULT, _payload(), meta)

    assert len(database.calls) == 1
    sql, args = database.calls[0]
    assert "INSERT INTO omninode_internal.board_probe_results" in sql
    assert args[6] == "probe-run-golden-chain"
    assert args[10] == _FINISHED_AT
    assert json.loads(args[9]) == ["probe://board/result"]
    assert published == [
        {
            "topic": TOPIC_EXPOSURE,
            "op": "upsert",
            "row": {
                "projection_cursor": 1,
                "check_id": "branch-protection",
                "subject_kind": "pull_request",
                "subject": "OmniNode-ai/omnimarket#321",
                "repo": "OmniNode-ai/omnimarket",
                "sha": "a" * 40,
                "surface_instance": "github-main",
                "execution_id": "probe-run-golden-chain",
                "outcome": "PASS",
                "reasons": [],
                "evidence_items": ["probe://board/result"],
                "finished_at": _FINISHED_AT.isoformat(),
                "source_offset": 41,
                "projected_at": published[0]["row"]["projected_at"],
            },
            "source_event_id": "board-probe-golden-chain",
            "source_topic": TOPIC_BOARD_PROBE_RESULT,
            "source_partition": 0,
            "source_offset": 41,
        }
    ]


def test_hop4_the_declared_outputs_close_the_chain() -> None:
    contract = _contract()

    assert (
        TOPIC_PROJECTION_APPLIED
        == "onex.evt.omnimarket.projection-board-probe-results-applied.v1"
    )
    assert contract["terminal_event"] == TOPIC_PROJECTION_APPLIED
    assert contract["event_bus"]["publish_topics"] == [TOPIC_PROJECTION_APPLIED]
    assert contract["externally_consumed_topics"] == [TOPIC_PROJECTION_APPLIED]
    assert contract["event_bus"]["dlq_topics"] == [TOPIC_DLQ]
    assert contract["projection_api"]["topic"] == TOPIC_EXPOSURE
    assert contract["projection_api"]["bus_backed"] is True
