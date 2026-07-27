# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Determinism tests for node_delivery_replay_projection_compute (OMN-14726, B6).

These tests are the B6 canary-acceptance proof: replaying the same event
sequence must yield the same projection checksum + cursor, and a divergent
sequence must differ. Everything runs in-process — there is no live bus/DB
dependency.

Coverage:
    - same sequence -> identical projection_checksum + cursor (fresh calls,
      JSON round-trip, correlation_id-independent)
    - mutated payload -> different projection_checksum (cursor unchanged)
    - reordered sequence -> different projection_checksum, SAME cursor token
      (the two signals are orthogonal)
    - dropped / added event -> different cursor token AND projection_checksum
    - re-offset (divergent delivery) -> different cursor token
    - expected-result comparison: match -> not diverged; mismatch -> diverged
      with the correct per-signal reasons
    - the async handler agrees with the pure function
    - model validation (frozen, extra=forbid, bounds)
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delivery_replay_projection_compute.handlers.handler_delivery_replay_projection import (
    HandlerDeliveryReplayProjection,
    project_delivery_sequence,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_event import (
    ModelDeliveryEvent,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_replay_input import (
    ModelDeliveryReplayInput,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_expectation import (
    ModelReplayExpectation,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    topic: str = "onex.evt.platform.node-registration.v1",
    partition: int = 0,
    offset: int,
    key: str = "node-a",
    event_type: str = "registered",
    payload: object = None,
) -> ModelDeliveryEvent:
    return ModelDeliveryEvent(
        topic=topic,
        partition=partition,
        offset=offset,
        key=key,
        event_type=event_type,
        payload=payload,
    )


def _sequence() -> tuple[ModelDeliveryEvent, ...]:
    return (
        _event(offset=0, key="node-a", payload={"status": "pending", "n": 1}),
        _event(offset=1, key="node-b", payload={"status": "pending", "n": 2}),
        _event(
            topic="onex.evt.platform.node-heartbeat.v1",
            partition=1,
            offset=0,
            key="node-a",
            event_type="heartbeat",
            payload={"seq": 10},
        ),
        _event(offset=2, key="node-a", payload={"status": "healthy", "n": 3}),
    )


# ---------------------------------------------------------------------------
# Determinism: same sequence -> identical checksum + cursor
# ---------------------------------------------------------------------------


class TestSameSequenceDeterminism:
    def test_repeated_calls_identical_projection(self) -> None:
        seq = _sequence()
        a = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        b = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        assert a.projection_checksum == b.projection_checksum
        assert a.cursor.token == b.cursor.token
        assert a.cursor == b.cursor
        assert a.event_count == len(seq) == 4

    def test_repeated_runs_are_stable(self) -> None:
        seq = _sequence()
        checksums = {
            project_delivery_sequence(
                ModelDeliveryReplayInput(sequence=seq)
            ).projection_checksum
            for _ in range(10)
        }
        assert len(checksums) == 1

    def test_correlation_id_does_not_affect_determinism(self) -> None:
        seq = _sequence()
        cid_a = uuid4()
        cid_b = uuid4()
        a = project_delivery_sequence(
            ModelDeliveryReplayInput(correlation_id=cid_a, sequence=seq)
        )
        b = project_delivery_sequence(
            ModelDeliveryReplayInput(correlation_id=cid_b, sequence=seq)
        )
        assert a.projection_checksum == b.projection_checksum
        assert a.cursor.token == b.cursor.token
        assert a.correlation_id == cid_a
        assert b.correlation_id == cid_b

    def test_json_round_trip_determinism(self) -> None:
        seq = _sequence()
        original = ModelDeliveryReplayInput(correlation_id=uuid4(), sequence=seq)
        reloaded = ModelDeliveryReplayInput.model_validate_json(
            original.model_dump_json()
        )
        assert (
            project_delivery_sequence(original).projection_checksum
            == project_delivery_sequence(reloaded).projection_checksum
        )
        assert (
            project_delivery_sequence(original).cursor.token
            == project_delivery_sequence(reloaded).cursor.token
        )


# ---------------------------------------------------------------------------
# Divergence: a divergent sequence must differ
# ---------------------------------------------------------------------------


class TestDivergentSequenceDiffers:
    def test_mutated_payload_changes_checksum(self) -> None:
        base = _sequence()
        mutated = (
            base[0],
            base[1],
            base[2],
            _event(offset=2, key="node-a", payload={"status": "DEGRADED", "n": 3}),
        )
        base_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=base))
        mut_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=mutated))
        assert base_r.projection_checksum != mut_r.projection_checksum
        # Same offsets + count -> cursor unchanged.
        assert base_r.cursor.token == mut_r.cursor.token

    def test_reorder_changes_checksum_but_not_cursor(self) -> None:
        base = _sequence()
        reordered = (base[1], base[0], base[3], base[2])
        base_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=base))
        reo_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=reordered))
        assert base_r.projection_checksum != reo_r.projection_checksum
        assert base_r.cursor.token == reo_r.cursor.token

    def test_dropped_event_changes_cursor_and_checksum(self) -> None:
        base = _sequence()
        dropped = base[:-1]
        base_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=base))
        drop_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=dropped))
        assert base_r.cursor.token != drop_r.cursor.token
        assert base_r.projection_checksum != drop_r.projection_checksum
        assert drop_r.event_count == len(base) - 1

    def test_added_event_changes_cursor_and_checksum(self) -> None:
        base = _sequence()
        extended = (*base, _event(offset=3, key="node-c", payload={"n": 99}))
        base_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=base))
        ext_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=extended))
        assert base_r.cursor.token != ext_r.cursor.token
        assert base_r.projection_checksum != ext_r.projection_checksum

    def test_reoffset_changes_cursor(self) -> None:
        base = _sequence()
        reoffset = (
            base[0],
            base[1],
            base[2],
            _event(offset=99, key="node-a", payload={"status": "healthy", "n": 3}),
        )
        base_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=base))
        ro_r = project_delivery_sequence(ModelDeliveryReplayInput(sequence=reoffset))
        assert base_r.cursor.token != ro_r.cursor.token
        assert base_r.projection_checksum != ro_r.projection_checksum

    def test_empty_sequence_is_deterministic(self) -> None:
        a = project_delivery_sequence(ModelDeliveryReplayInput(sequence=()))
        b = project_delivery_sequence(ModelDeliveryReplayInput(sequence=()))
        assert a.projection_checksum == b.projection_checksum
        assert a.cursor.token == b.cursor.token
        assert a.event_count == 0
        assert a.cursor.positions == ()


# ---------------------------------------------------------------------------
# Cursor structure
# ---------------------------------------------------------------------------


class TestCursorStructure:
    def test_cursor_records_max_offset_per_partition(self) -> None:
        result = project_delivery_sequence(
            ModelDeliveryReplayInput(sequence=_sequence())
        )
        positions = {
            (pos.topic, pos.partition): pos.offset for pos in result.cursor.positions
        }
        assert positions[("onex.evt.platform.node-registration.v1", 0)] == 2
        assert positions[("onex.evt.platform.node-heartbeat.v1", 1)] == 0

    def test_cursor_positions_sorted_deterministically(self) -> None:
        seq = _sequence()
        forward = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        shuffled = project_delivery_sequence(
            ModelDeliveryReplayInput(sequence=(seq[3], seq[2], seq[1], seq[0]))
        )
        assert forward.cursor.positions == shuffled.cursor.positions


# ---------------------------------------------------------------------------
# Expected-result comparison
# ---------------------------------------------------------------------------


class TestExpectedComparison:
    def test_matching_expectation_not_diverged(self) -> None:
        seq = _sequence()
        truth = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        compared = project_delivery_sequence(
            ModelDeliveryReplayInput(
                sequence=seq,
                expected=ModelReplayExpectation(
                    projection_checksum=truth.projection_checksum,
                    cursor_token=truth.cursor.token,
                ),
            )
        )
        assert compared.compared is True
        assert compared.diverged is False
        assert compared.divergence_reasons == ()

    def test_no_expectation_reports_not_compared(self) -> None:
        result = project_delivery_sequence(
            ModelDeliveryReplayInput(sequence=_sequence())
        )
        assert result.compared is False
        assert result.diverged is False
        assert result.divergence_reasons == ()

    def test_checksum_only_divergence(self) -> None:
        seq = _sequence()
        truth = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        result = project_delivery_sequence(
            ModelDeliveryReplayInput(
                sequence=seq,
                expected=ModelReplayExpectation(
                    projection_checksum="0" * 64,
                    cursor_token=truth.cursor.token,
                ),
            )
        )
        assert result.diverged is True
        assert result.divergence_reasons == ("projection_checksum",)

    def test_cursor_only_divergence(self) -> None:
        seq = _sequence()
        truth = project_delivery_sequence(ModelDeliveryReplayInput(sequence=seq))
        result = project_delivery_sequence(
            ModelDeliveryReplayInput(
                sequence=seq,
                expected=ModelReplayExpectation(
                    projection_checksum=truth.projection_checksum,
                    cursor_token='{"event_count":0,"positions":[]}',
                ),
            )
        )
        assert result.diverged is True
        assert result.divergence_reasons == ("cursor",)

    def test_both_signals_diverge(self) -> None:
        seq = _sequence()
        result = project_delivery_sequence(
            ModelDeliveryReplayInput(
                sequence=seq,
                expected=ModelReplayExpectation(
                    projection_checksum="0" * 64,
                    cursor_token="not-a-real-token",
                ),
            )
        )
        assert result.diverged is True
        assert set(result.divergence_reasons) == {"projection_checksum", "cursor"}


# ---------------------------------------------------------------------------
# Async handler parity
# ---------------------------------------------------------------------------


class TestAsyncHandler:
    def test_handler_matches_pure_function(self) -> None:
        seq = _sequence()
        inp = ModelDeliveryReplayInput(sequence=seq)
        handler = HandlerDeliveryReplayProjection()
        via_handler = asyncio.run(handler.handle(inp))
        via_function = project_delivery_sequence(inp)
        assert via_handler == via_function

    def test_handler_classification(self) -> None:
        handler = HandlerDeliveryReplayProjection()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    def test_delivery_event_is_frozen(self) -> None:
        event = _event(offset=0)
        with pytest.raises(ValidationError):
            event.offset = 5  # type: ignore[misc]

    def test_delivery_event_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ModelDeliveryEvent(
                topic="t",
                offset=0,
                event_type="e",
                bogus="x",  # type: ignore[call-arg]
            )

    def test_negative_offset_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _event(offset=-1)

    def test_empty_topic_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _event(topic="", offset=0)
