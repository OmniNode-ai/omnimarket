# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A RELEASE or HOLD addressed to a running lane reaches its spool, as a MSG does.

The recorded case: a lane was briefed to push after the orchestrator's RELEASE.
The lane's inbox was read once at launch, and the spool read only the MSG topic
and refused every row that was not a MSG, so the RELEASE appended while the lane
ran never reached it and the lane ended partial. The RELEASE row below has the
shape the landing controller reads (``re=`` the HOLD id, ``pr=`` the named PR),
addressed ``to=`` the lane.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_agent_inbox_spool import HandlerAgentInboxSpool
from omnimarket.nodes.node_agent_inbox_spool.handlers.parse_ledger_msg import (
    MalformedLedgerMsgError,
    decode_msg_record,
)
from omnimarket.nodes.node_agent_inbox_spool.models import ModelAgentInboxSpoolRequest
from omnimarket.nodes.node_agent_inbox_spool.node import load_topics

from .helpers import RecordingBus, msg_row, overlay, record

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 3, 23, 40, tzinfo=UTC)
LANE = "relay-lane-a"
HOLD_ID = "2026-11-03T23:00:00Z-orchestrator"
MSG_TOPIC = "onex.evt.omnimarket.work-ledger-msg.v2"
RELEASE_TOPIC = "onex.evt.omnimarket.work-ledger-release.v2"
HOLD_TOPIC = "onex.evt.omnimarket.work-ledger-hold.v2"

RELEASE_ROW = (
    f"2026-11-03T23:33:18Z | RELEASE | lane=orchestrator | to={LANE} | re={HOLD_ID}"
    " | pr=OmniNode-ai/example#12 | ticket=OMN-1 | push now"
)
HOLD_ROW = (
    f"2026-11-03T23:00:00Z | HOLD | lane=orchestrator | id={HOLD_ID} | to={LANE}"
    " | until=2026-11-05T00:00:00Z | ticket=OMN-1 | do not push yet"
)
UNADDRESSED_RELEASE = (
    "2026-11-03T23:34:00Z | RELEASE | lane=orchestrator | surface=slot-1 | result=CLEAN"
    " | slot torn down"
)


def test_agent_inbox_spool_reads_release_and_hold_topics() -> None:
    assert load_topics() == (MSG_TOPIC, RELEASE_TOPIC, HOLD_TOPIC)


def test_agent_inbox_spool_decodes_release_and_hold_rows() -> None:
    release = decode_msg_record(record(RELEASE_ROW), "group")
    assert release.row_type == "RELEASE"
    assert release.recipients == (LANE,)
    assert release.ticket == "OMN-1"
    assert release.text == "push now"
    hold = decode_msg_record(record(HOLD_ROW), "group")
    assert (hold.row_type, hold.msg_id, hold.recipients) == ("HOLD", HOLD_ID, (LANE,))
    assert decode_msg_record(record(UNADDRESSED_RELEASE), "group").recipients == ()
    assert decode_msg_record(record(msg_row(LANE, "n")), "group").row_type == "MSG"
    with pytest.raises(MalformedLedgerMsgError):
        decode_msg_record(
            record("2026-11-03T23:00:00Z | STATUS | lane=x | to=a | hi"), "group"
        )


async def test_agent_inbox_spool_delivers_a_release_to_a_running_lane(
    tmp_path: Path,
) -> None:
    bus = EventBusInmemory(environment="test", group="spool-test")
    await bus.start()
    try:
        handler = HandlerAgentInboxSpool(bus=RecordingBus(bus), clock=lambda: NOW)
        req = ModelAgentInboxSpoolRequest(
            host="host-a", lanes=(LANE,), overlay=overlay(tmp_path)
        )
        # At launch the lane holds a HOLD and nothing else.
        await bus.publish(HOLD_TOPIC, None, record(HOLD_ROW))
        first = await handler.handle(req)
        # While the lane runs, the orchestrator appends the RELEASE.
        await bus.publish(RELEASE_TOPIC, None, record(RELEASE_ROW))
        await bus.publish(RELEASE_TOPIC, None, record(UNADDRESSED_RELEASE))
        second = await handler.handle(req)
    finally:
        await bus.close()
    assert first.read_ok, first.error
    assert second.read_ok, second.error
    assert len(first.spooled) == 1
    assert len(second.spooled) == 1
    assert second.malformed == 0
    assert second.unaddressed == 1
    assert second.unread_lanes == (LANE,)
    spooled = [
        json.loads(p.read_text()) for p in (tmp_path / LANE / "msgs").glob("*.json")
    ]
    assert sorted(str(e["row_type"]) for e in spooled) == ["HOLD", "RELEASE"]
    release = next(e for e in spooled if e["row_type"] == "RELEASE")
    assert release["raw_row"] == RELEASE_ROW
