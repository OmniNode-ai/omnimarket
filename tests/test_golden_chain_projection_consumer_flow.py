# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_consumer_flow`` (OMN-16777).

Walks the chain the contract declares, hop by hop, with the incident's own
numbers:

    onex.evt.platform.node-heartbeat.v1  (flow_window: raw counters)
        -> topic_produce_windows          (upstream-production evidence)
        -> consumer_flow_windows          (verdict derived here, not upstream)
        -> onex.snapshot.projection.consumer-flow.v1   (bus-backed exposure)
        -> onex.evt.omnimarket.projection-consumer-flow-applied.v1  (terminal)

The chain's whole purpose is one distinction: 15,750 messages in and 0 out must
not look the same as 15,750 in and 15,750 out. Everything else here is in
service of that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    HandlerProjectionConsumerFlow,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    ModelConsumerFlowProjectionRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)

_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: the carrier the runtime already emits on
_SNAPSHOT_TOPIC = "onex.snapshot.projection.consumer-flow.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-consumer-flow-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-consumer-flow-malformed.v1"  # onex-topic-allow: this node's declared DLQ

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_STALLED_GROUP = "onex-dev.omnimarket.gateway-link-health-projection-compute.consume"


def _contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_golden_chain_hops_are_the_declared_topics() -> None:
    """Every hop in the chain is contract-declared — none is a code constant.

    A topic that lives only in Python is a topic the platform cannot reason
    about, and this node exists precisely to stop the platform being unable to
    reason about its own plumbing.
    """
    contract = _contract()
    event_bus = contract["event_bus"]

    assert event_bus["subscribe_topics"] == [_HEARTBEAT_TOPIC], (
        "the chain must ride the heartbeat the runtime ALREADY emits — a new "
        "transport would keep reporting on a runtime that has already died"
    )
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["externally_consumed_topics"] == [_TERMINAL_TOPIC]


@pytest.mark.unit
def test_golden_chain_end_to_end_separates_stalled_from_flowing() -> None:
    """The chain, walked with the OMN-16755 numbers alongside a live control.

    Both consumers below are Stable, both are at LAG 0 when the window closes,
    both processed every message handed to them. Before this node the platform
    had no surface on which they differed at all.
    """
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)
    out_topic = "onex.cmd.omnibase-infra.gateway-link-health-upsert.v1"  # onex-topic-allow: the incident's own output topic

    def _delta(group: str, in_: int, out: int) -> dict[str, Any]:
        return {
            "consumer_group": group,
            "topic": _HEARTBEAT_TOPIC,
            "node_id": node_id,
            "window_start": _T0.isoformat(),
            "window_end": end.isoformat(),
            "window_sequence": 1,
            "messages_in": in_,
            "messages_out": out,
            "messages_dlq": 0,
            "handler_errors": 0,
        }

    # The heartbeat payload EXACTLY as it arrives on the wire — every field the
    # runtime puts on ModelNodeHeartbeatEvent, not a hand-trimmed subset, so the
    # request model's extra="ignore" is exercised rather than assumed.
    request = ModelConsumerFlowProjectionRequest.model_validate(
        {
            "node_id": node_id,
            "node_type": "COMPUTE",
            "uptime_seconds": 3600.0,
            "active_operations_count": 0,
            "timestamp": end.isoformat(),
            "flow_window": {
                "node_id": node_id,
                "window_start": _T0.isoformat(),
                "window_end": end.isoformat(),
                "window_sequence": 1,
                "consumer_deltas": [
                    _delta(_STALLED_GROUP, 15750, 0),
                    _delta(
                        "onex-dev.omnimarket.live-events-writer.consume", 15750, 15750
                    ),
                ],
                "produce_deltas": [
                    {
                        "topic": out_topic,
                        "node_id": node_id,
                        "window_start": _T0.isoformat(),
                        "window_end": end.isoformat(),
                        "window_sequence": 1,
                        "messages_produced": 15750,
                    }
                ],
            },
            # The two database-resolved facts the writer hands in.
            "upstream_produced_by_topic": {out_topic: 15750},
            "last_observed_sequence": 0,
            "known_keys": [],
        }
    )

    result = HandlerProjectionConsumerFlow().handle(request)

    # Hop 1: the produce tally rides through as the upstream-production
    # evidence the next hop's verdict is allowed to consult.
    assert [row.topic for row in result.produce_rows] == [out_topic]
    assert result.produce_rows[0].messages_produced == 15750

    # Hop 2: two rows, two verdicts, from one event.
    rows = {row.consumer_group: row for row in result.flow_rows}
    assert len(rows) == 2
    assert rows[_STALLED_GROUP].flow_state is EnumConsumerFlowState.STALLED, (
        "the 15,750-in / 0-out consumer did not surface as STALLED — this is "
        "the OMN-16755 case and the entire reason the chain exists"
    )
    assert (
        rows["onex-dev.omnimarket.live-events-writer.consume"].flow_state
        is EnumConsumerFlowState.FLOWING
    )

    # Hop 3: the derived row carries every column the snapshot exposure
    # declares, so the bus-backed publish has a complete row to send.
    serialized = rows[_STALLED_GROUP].model_dump(mode="json")
    for column in _contract()["projection_api"]["columns"]:
        assert column in serialized, (
            f"exposure declares column {column!r} that the derivation never "
            "sets — the served row would carry a hole"
        )

    # Hop 4: the terminal the contract declares is the one this chain ends on.
    assert _contract()["terminal_event"] == _TERMINAL_TOPIC


@pytest.mark.unit
def test_a_heartbeat_without_a_window_implies_no_rows() -> None:
    """The priming tick and the non-carrier node both send a heartbeat with no
    window. Absence is not zero traffic, so it must imply nothing at all."""
    result = HandlerProjectionConsumerFlow().handle(
        ModelConsumerFlowProjectionRequest.model_validate(
            {"node_id": str(uuid4()), "uptime_seconds": 1.0}
        )
    )
    assert result.flow_rows == ()
    assert result.produce_rows == ()
    assert result.unknown_rows == ()
