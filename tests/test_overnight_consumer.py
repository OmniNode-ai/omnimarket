# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_overnight Kafka consumer wiring (OMN-10535)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_overnight.consumer import (
    _DEFAULT_GROUP,
    TOPIC_OVERNIGHT_COMPLETED,
    TOPIC_OVERNIGHT_FAILED,
    TOPIC_OVERNIGHT_START,
    _build_failure_payload,
    _parse_command,
)


@pytest.mark.unit
class TestOvernightConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_start_topic(self) -> None:
        assert TOPIC_OVERNIGHT_START == "onex.cmd.omnimarket.overnight-start.v1"

    def test_completed_topic(self) -> None:
        assert (
            TOPIC_OVERNIGHT_COMPLETED
            == "onex.evt.omnimarket.overnight-session-completed.v1"
        )

    def test_failed_topic(self) -> None:
        assert (
            TOPIC_OVERNIGHT_FAILED == "onex.evt.omnimarket.overnight-session-failed.v1"
        )

    def test_default_consumer_group(self) -> None:
        assert _DEFAULT_GROUP == "local.omnimarket.overnight.consume.1.0.0"


@pytest.mark.unit
class TestOvernightParseCommand:
    """_parse_command extracts and defaults fields from raw Kafka payloads."""

    def test_defaults_when_empty_payload(self) -> None:
        cmd = _parse_command({})
        assert cmd["max_cycles"] == 0
        assert cmd["skip_nightly_loop"] is False
        assert cmd["skip_build_loop"] is False
        assert cmd["skip_merge_sweep"] is False
        assert cmd["dry_run"] is False
        assert cmd["enable_self_loop"] is True
        assert cmd["loop_delay_seconds"] == 300
        assert isinstance(cmd["correlation_id"], str)
        assert len(cmd["correlation_id"]) > 0

    def test_explicit_values_override_defaults(self) -> None:
        cmd = _parse_command(
            {
                "correlation_id": "night-abc-123",
                "max_cycles": 3,
                "skip_build_loop": True,
                "skip_nightly_loop": True,
                "dry_run": True,
                "enable_self_loop": False,
                "loop_delay_seconds": 60,
            }
        )
        assert cmd["correlation_id"] == "night-abc-123"
        assert cmd["max_cycles"] == 3
        assert cmd["skip_build_loop"] is True
        assert cmd["skip_nightly_loop"] is True
        assert cmd["dry_run"] is True
        assert cmd["enable_self_loop"] is False
        assert cmd["loop_delay_seconds"] == 60

    def test_correlation_id_generated_when_missing(self) -> None:
        cmd1 = _parse_command({})
        cmd2 = _parse_command({})
        assert cmd1["correlation_id"] != cmd2["correlation_id"]

    def test_max_cycles_coerced_from_string(self) -> None:
        cmd = _parse_command({"max_cycles": "5"})
        assert cmd["max_cycles"] == 5

    def test_dry_run_coerced_from_int(self) -> None:
        cmd = _parse_command({"dry_run": 1})
        assert cmd["dry_run"] is True

    def test_failure_event_shape(self) -> None:
        failure = _build_failure_payload("night-corr-001", RuntimeError("phase error"))
        assert failure["correlation_id"] == "night-corr-001"
        assert "error" in failure
        assert "phase" in failure
        assert "failed_at" in failure
        assert failure["phase"] == "overnight"
