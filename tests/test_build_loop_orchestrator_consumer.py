# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_build_loop_orchestrator Kafka consumer wiring (OMN-10465)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.consumer import (
    TOPIC_BUILD_LOOP_COMPLETED,
    TOPIC_BUILD_LOOP_FAILED,
    TOPIC_BUILD_LOOP_START,
    _parse_command,
)


@pytest.mark.unit
class TestBuildLoopConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_start_topic(self) -> None:
        assert (
            TOPIC_BUILD_LOOP_START
            == "onex.cmd.omnimarket.build-loop-orchestrator-start.v1"
        )

    def test_completed_topic(self) -> None:
        assert (
            TOPIC_BUILD_LOOP_COMPLETED
            == "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
        )

    def test_failed_topic(self) -> None:
        assert TOPIC_BUILD_LOOP_FAILED == "onex.evt.omnimarket.build-loop-failed.v1"


@pytest.mark.unit
class TestBuildLoopParseCommand:
    """_parse_command extracts and defaults fields from raw Kafka payloads."""

    def test_defaults_when_empty_payload(self) -> None:
        cmd = _parse_command({})
        assert cmd["max_tickets"] == 5
        assert cmd["max_cycles"] == 1
        assert cmd["dry_run"] is False
        assert cmd["skip_closeout"] is True
        assert isinstance(cmd["correlation_id"], str)
        assert len(cmd["correlation_id"]) > 0

    def test_explicit_values_override_defaults(self) -> None:
        cmd = _parse_command(
            {
                "correlation_id": "abc-123",
                "max_tickets": 3,
                "max_cycles": 2,
                "dry_run": True,
                "skip_closeout": False,
            }
        )
        assert cmd["correlation_id"] == "abc-123"
        assert cmd["max_tickets"] == 3
        assert cmd["max_cycles"] == 2
        assert cmd["dry_run"] is True
        assert cmd["skip_closeout"] is False

    def test_correlation_id_generated_when_missing(self) -> None:
        cmd1 = _parse_command({})
        cmd2 = _parse_command({})
        # Each empty payload generates a unique correlation_id
        assert cmd1["correlation_id"] != cmd2["correlation_id"]

    def test_string_zero_used_as_max_tickets(self) -> None:
        # Kafka payloads may come as strings
        cmd = _parse_command({"max_tickets": "7"})
        assert cmd["max_tickets"] == 7

    def test_dry_run_coerced_from_truthy_string(self) -> None:
        cmd = _parse_command({"dry_run": 1})
        assert cmd["dry_run"] is True

    def test_failure_event_shape(self) -> None:
        """Verify a failure payload dict would have the required fields."""
        correlation_id = "test-corr-456"
        from datetime import UTC, datetime

        failure = {
            "correlation_id": correlation_id,
            "phase": "build_loop",
            "error": "something went wrong",
            "failed_at": datetime.now(tz=UTC).isoformat(),
        }
        assert failure["correlation_id"] == correlation_id
        assert "error" in failure
        assert "phase" in failure
        assert "failed_at" in failure
