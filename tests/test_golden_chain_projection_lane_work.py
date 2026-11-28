# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the lane work projection (OMN-26708).

Walks the chain the contract's ``golden_path`` declares: a transition receipt
arrives on the declared topic, the writer upserts its workflow row, and the row
leaves as a keyed snapshot delta on the declared exposures, a terminal row also on
the terminal one. The publisher and the database are doubles; whether a broker
and a Postgres accepted the bytes is the lab proof.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_lane_work.contract_topics import (
    TOPIC_EXPOSURE_TERMINAL,
    TOPIC_EXPOSURE_WORKFLOW,
    TOPIC_LANE_WORK_TRANSITION,
    TOPIC_PROJECTION_APPLIED,
)
from omnimarket.nodes.node_projection_lane_work.handlers.handler_projection_lane_work import (
    HandlerProjectionLaneWork,
    LaneWorkProjectionWriter,
)
from tests.unit.nodes.node_projection_lane_work.receipts import handed_off_workflow
from tests.unit.nodes.node_projection_lane_work.test_lane_work_writer import _FakeDb

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_lane_work/contract.yaml"
)


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load(CONTRACT.read_text()))


def test_hop1_the_contract_subscribes_to_the_transition_topic_and_names_its_producer() -> (
    None
):
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [TOPIC_LANE_WORK_TRANSITION]
    produced = {e["topic"] for e in contract["externally_produced_topics"]}
    assert produced == {TOPIC_LANE_WORK_TRANSITION}


def test_hop2_the_pure_fold_renders_the_row_a_receipt_describes() -> None:
    result = HandlerProjectionLaneWork().handle(handed_off_workflow("b-2-1")[-1])
    assert result.rows[0]["terminal_kind"] == "handed_off"


def test_hop3_the_writer_republishes_the_row_and_the_terminal_declares_its_event() -> (
    None
):
    writer = LaneWorkProjectionWriter()
    cast(Any, writer)._db = _FakeDb()
    published: list[str] = []

    async def _capture(exposure: Any, **_kwargs: Any) -> bool:
        published.append(exposure.topic)
        return True

    async def _no_producer() -> None:
        return None

    cast(Any, writer).publish_snapshot_delta = _capture
    cast(Any, writer)._stop_producer = _no_producer
    for offset, receipt in enumerate(handed_off_workflow("b-2-1")):
        writer.project_request(
            ModelProjectionRequest(
                topic=TOPIC_LANE_WORK_TRANSITION,
                partition=0,
                offset=offset,
                has_coordinates=True,
                fallback_id=f"o{offset}",
                payload=receipt.model_dump(mode="json"),
            )
        )
    assert published.count(TOPIC_EXPOSURE_WORKFLOW) == 8
    assert published.count(TOPIC_EXPOSURE_TERMINAL) == 1
    assert _contract()["terminal_event"] == TOPIC_PROJECTION_APPLIED
    assert (
        _contract()["terminal_event"]
        == "onex.evt.omnimarket.projection-lane-work-applied.v1"
    )
