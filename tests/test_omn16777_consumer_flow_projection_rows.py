# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16777 — what one heartbeat window implies, before anything is written.

The derivation is a pure def-B function: a typed request in, typed rows out, no
clock and no database. These drive it directly with the incident's own numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    HandlerProjectionConsumerFlow,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    EnumUpstreamEvidence,
    ModelConsumerFlowProjectionRequest,
)

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_OUT_TOPIC = "onex.cmd.omnibase-infra.gateway-link-health-upsert.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_STALLED_GROUP = "onex-dev.omnimarket.gateway-link-health-projection-compute.consume"


def _delta(
    *,
    group: str,
    node_id: str,
    sequence: int = 1,
    start: datetime = _T0,
    end: datetime | None = None,
    topic: str = _IN_TOPIC,
    messages_in: int = 0,
    messages_out: int = 0,
    messages_dlq: int = 0,
    handler_errors: int = 0,
) -> dict[str, Any]:
    return {
        "consumer_group": group,
        "topic": topic,
        "node_id": node_id,
        "window_start": start.isoformat(),
        "window_end": (end or start + timedelta(seconds=60)).isoformat(),
        "window_sequence": sequence,
        "messages_in": messages_in,
        "messages_out": messages_out,
        "messages_dlq": messages_dlq,
        "handler_errors": handler_errors,
    }


def _request(
    *,
    node_id: str,
    sequence: int = 1,
    start: datetime = _T0,
    consumer_deltas: list[dict[str, Any]],
    produce_deltas: list[dict[str, Any]] | None = None,
    upstream: dict[str, int] | None = None,
    last_observed_sequence: int | None = None,
    known_keys: list[list[str]] | None = None,
) -> ModelConsumerFlowProjectionRequest:
    return ModelConsumerFlowProjectionRequest.model_validate(
        {
            "node_id": node_id,
            "uptime_seconds": 1.0,
            "flow_window": {
                "node_id": node_id,
                "window_start": start.isoformat(),
                "window_end": (start + timedelta(seconds=60)).isoformat(),
                "window_sequence": sequence,
                "consumer_deltas": consumer_deltas,
                "produce_deltas": produce_deltas or [],
            },
            "upstream_produced_by_topic": upstream or {},
            "last_observed_sequence": last_observed_sequence,
            "known_keys": known_keys or [],
        }
    )


@pytest.mark.unit
def test_stalled_row_carries_its_counters_and_its_verdict() -> None:
    """AC2 end to end: 15,750 in / 0 out derives a STALLED row."""
    node_id = str(uuid4())
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            consumer_deltas=[
                _delta(
                    group=_STALLED_GROUP,
                    node_id=node_id,
                    messages_in=15750,
                    messages_out=0,
                )
            ],
        )
    )
    (row,) = result.flow_rows
    assert row.flow_state is EnumConsumerFlowState.STALLED
    assert row.messages_in == 15750
    assert row.messages_out == 0


@pytest.mark.unit
def test_two_legs_are_separate_rows() -> None:
    """AC3's actual content: the OMN-16754 defect was ONE verdict spanning two
    legs, so a live outbound leg vouched for a dead inbound one."""
    node_id = str(uuid4())
    inbound = "onex-dev.gateway-forwarder.inbound.consume"
    outbound = "onex-dev.gateway-forwarder.outbound.consume"
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            consumer_deltas=[
                _delta(group=inbound, node_id=node_id),
                _delta(
                    group=outbound,
                    node_id=node_id,
                    messages_in=5575,
                    messages_out=5575,
                ),
            ],
        )
    )
    rows = {row.consumer_group: row for row in result.flow_rows}
    assert len(rows) == 2, "the two legs collapsed into one row"
    assert rows[outbound].flow_state is EnumConsumerFlowState.FLOWING
    # The inbound leg is fed from OUTSIDE this runtime, so nothing here produces
    # to its topic and there is no honest STARVED evidence. It reports IDLE and
    # records WHY — never a fabricated STARVED, which would fire on every quiet
    # externally-fed topic in the platform.
    assert rows[inbound].flow_state is EnumConsumerFlowState.IDLE
    assert rows[inbound].upstream_evidence is EnumUpstreamEvidence.NONE
    assert rows[inbound].flow_state is not rows[outbound].flow_state, (
        "one leg's health was allowed to speak for the other"
    )


@pytest.mark.unit
def test_upstream_production_makes_a_silent_consumer_starved() -> None:
    """A consumer that took nothing on a topic this runtime WAS publishing to."""
    node_id = str(uuid4())
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            consumer_deltas=[
                _delta(
                    group="onex-dev.omnimarket.link-health-writer.consume",
                    node_id=node_id,
                    topic=_OUT_TOPIC,
                )
            ],
            upstream={_OUT_TOPIC: 12},
        )
    )
    (row,) = result.flow_rows
    assert row.flow_state is EnumConsumerFlowState.STARVED
    assert row.upstream_produced == 12


@pytest.mark.unit
def test_missed_window_derives_unknown_with_none_counters() -> None:
    """AC5: ``UNKNOWN != 0 messages``.

    The node last delivered window 1; window 3 arrives. Window 2 existed and was
    lost, and it must materialize with NULL counters. A zero row would read as
    "this consumer was alive and idle", so a runtime that stopped heartbeating
    entirely would report permanently healthy — the defect this epic closes.
    """
    node_id = str(uuid4())
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            sequence=3,
            start=_T0 + timedelta(seconds=120),
            consumer_deltas=[
                _delta(
                    group=_STALLED_GROUP,
                    node_id=node_id,
                    sequence=3,
                    start=_T0 + timedelta(seconds=120),
                    messages_in=10,
                    messages_out=10,
                )
            ],
            last_observed_sequence=1,
            known_keys=[[_STALLED_GROUP, _IN_TOPIC]],
        )
    )
    (gap,) = result.unknown_rows
    assert gap.flow_state is EnumConsumerFlowState.UNKNOWN
    assert gap.ingest_sequence == 2
    assert gap.messages_in is None, (
        "the missed window derived 0 messages; UNKNOWN must never be readable "
        "as observed-idle (AC5)"
    )
    assert gap.messages_out is None
    assert gap.messages_dlq is None
    assert gap.handler_errors is None
    assert gap.messages_in != 0


@pytest.mark.unit
def test_a_contiguous_sequence_derives_no_unknown_row() -> None:
    """The false-positive half of AC5: an unbroken sequence invents no gap."""
    node_id = str(uuid4())
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            sequence=2,
            start=_T0 + timedelta(seconds=60),
            consumer_deltas=[
                _delta(
                    group=_STALLED_GROUP,
                    node_id=node_id,
                    sequence=2,
                    start=_T0 + timedelta(seconds=60),
                    messages_in=1,
                    messages_out=1,
                )
            ],
            last_observed_sequence=1,
            known_keys=[[_STALLED_GROUP, _IN_TOPIC]],
        )
    )
    assert result.unknown_rows == ()


@pytest.mark.unit
def test_replaying_the_same_request_derives_identical_rows() -> None:
    """AC6: replay determinism. No wall clock anywhere in the derivation."""
    node_id = str(uuid4())

    def _run() -> str:
        result = HandlerProjectionConsumerFlow().handle(
            _request(
                node_id=node_id,
                consumer_deltas=[
                    _delta(
                        group=_STALLED_GROUP,
                        node_id=node_id,
                        messages_in=15750,
                        messages_out=0,
                        messages_dlq=3,
                        handler_errors=3,
                    )
                ],
            )
        )
        return result.model_dump_json()

    assert _run() == _run()


@pytest.mark.unit
def test_evaluated_at_is_event_time_not_a_wall_clock() -> None:
    """The row is a statement ABOUT the window, so it is stamped with the
    window's own end. A wall clock here would make replay non-deterministic and
    would quietly turn ordering into an ingest-time property."""
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)
    result = HandlerProjectionConsumerFlow().handle(
        _request(
            node_id=node_id,
            consumer_deltas=[
                _delta(group=_STALLED_GROUP, node_id=node_id, messages_in=1)
            ],
        )
    )
    (row,) = result.flow_rows
    assert row.evaluated_at == end
    assert row.window_end == end
