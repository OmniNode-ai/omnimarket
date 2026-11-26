# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the floor panel answer node, and its contract's seams (OMN-19998)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

from omnimarket.models.floor_panel import EnumFloorPanelCode
from omnimarket.models.work_state_panel import EnumPanelState
from omnimarket.nodes.node_floor_panel_answer_effect.handlers.handler_floor_panel_answer import (
    HandlerFloorPanelAnswer,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    RaisingFloorPanelRowReader,
)
from tests.nodes.node_floor_panel_answer_effect.support import (
    ADDRESS,
    CONTRACT,
    MARKER,
    PANEL_ID,
    SRC_NODES,
    T0,
    command,
    handler,
    held_row,
    reader_with,
)

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.floor-panel.v1"
ANSWERED_TOPIC = "onex.evt.omnimarket.floor-panel-answered.v1"


async def test_golden_chain_a_held_panel_is_answered_read_with_its_body() -> None:
    answer = await handler(
        reader_with(held_row(T0), 4), T0 + timedelta(seconds=30)
    ).handle(command(ADDRESS))
    assert answer.state is EnumPanelState.READ
    assert answer.panel == PANEL_ID
    assert answer.correlation_id == "c-1"
    assert answer.row_version == 4
    assert answer.epoch == 1
    assert answer.body is not None


@pytest.mark.parametrize(
    ("payload", "reader_kind", "code"),
    [
        ({MARKER: MARKER}, "memory", EnumFloorPanelCode.REQUEST_INVALID),
        ({"panel": MARKER}, "memory", EnumFloorPanelCode.UNKNOWN),
        (ADDRESS, "raising", EnumFloorPanelCode.STORE_UNAVAILABLE),
    ],
)
async def test_error_chain_each_named_answer_keeps_the_correlation_id(
    payload: dict[str, object], reader_kind: str, code: EnumFloorPanelCode
) -> None:
    reader = (
        RaisingFloorPanelRowReader()
        if reader_kind == "raising"
        else reader_with(held_row(T0), 1)
    )
    answer = await handler(reader, T0).handle(command(payload))
    assert answer.code is code
    assert answer.correlation_id == "c-1"
    assert MARKER not in answer.model_dump_json()


async def test_error_chain_an_absent_overlay_is_entry_invalid() -> None:
    answer = await HandlerFloorPanelAnswer(clock=lambda: T0, environ={}).handle(
        command(ADDRESS)
    )
    assert answer.code is EnumFloorPanelCode.ENTRY_INVALID
    assert answer.panel is None


def test_the_handler_resolves_with_no_arguments() -> None:
    assert HandlerFloorPanelAnswer().handler_category == "EFFECT"


def test_contract_topics_and_no_second_publisher_of_the_answer_topic() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["terminal_event"] == ANSWERED_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert ANSWERED_TOPIC in contract["event_bus"]["publish_topics"]
    publishers = sorted(
        path.parent.name
        for path in SRC_NODES.glob("*/contract.yaml")
        if ANSWERED_TOPIC
        in (
            yaml.safe_load(path.read_text(encoding="utf-8")).get("event_bus") or {}
        ).get("publish_topics", [])
    )
    assert publishers == ["node_floor_panel_answer_effect"]
