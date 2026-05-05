# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_autopilot_orchestrator Kafka consumer wiring (OMN-10535)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_autopilot_orchestrator.consumer import (
    _DEFAULT_GROUP,
    TOPIC_AUTOPILOT_COMPLETED,
    TOPIC_AUTOPILOT_FAILED,
    TOPIC_AUTOPILOT_START,
    _build_failure_payload,
    _parse_command,
)


@pytest.mark.unit
class TestAutopilotOrchestratorConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_start_topic(self) -> None:
        assert (
            TOPIC_AUTOPILOT_START
            == "onex.cmd.omnimarket.autopilot-orchestrator-start.v1"
        )

    def test_completed_topic(self) -> None:
        assert (
            TOPIC_AUTOPILOT_COMPLETED
            == "onex.evt.omnimarket.autopilot-orchestrator-completed.v1"
        )

    def test_failed_topic(self) -> None:
        assert (
            TOPIC_AUTOPILOT_FAILED
            == "onex.evt.omnimarket.autopilot-orchestrator-failed.v1"
        )

    def test_default_consumer_group(self) -> None:
        assert _DEFAULT_GROUP == "local.omnimarket.autopilot_orchestrator.consume.1.0.0"


@pytest.mark.unit
class TestAutopilotOrchestratorParseCommand:
    """_parse_command extracts and defaults fields from raw Kafka payloads."""

    def test_defaults_when_empty_payload(self) -> None:
        cmd = _parse_command({})
        assert cmd["mode"] == "close-out"
        assert cmd["dry_run"] is False
        assert cmd["autonomous"] is True
        assert isinstance(cmd["correlation_id"], str)
        assert len(cmd["correlation_id"]) > 0

    def test_explicit_values_override_defaults(self) -> None:
        cmd = _parse_command(
            {
                "correlation_id": "auto-abc-123",
                "mode": "build",
                "dry_run": True,
                "autonomous": False,
            }
        )
        assert cmd["correlation_id"] == "auto-abc-123"
        assert cmd["mode"] == "build"
        assert cmd["dry_run"] is True
        assert cmd["autonomous"] is False

    def test_correlation_id_generated_when_missing(self) -> None:
        cmd1 = _parse_command({})
        cmd2 = _parse_command({})
        assert cmd1["correlation_id"] != cmd2["correlation_id"]

    def test_dry_run_coerced_from_int(self) -> None:
        cmd = _parse_command({"dry_run": 1})
        assert cmd["dry_run"] is True

    def test_failure_event_shape(self) -> None:
        failure = _build_failure_payload(
            "auto-corr-001", RuntimeError("phase A failed")
        )
        assert failure["correlation_id"] == "auto-corr-001"
        assert "error" in failure
        assert "phase" in failure
        assert "failed_at" in failure
        assert failure["phase"] == "autopilot_orchestrator"
