# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_session_orchestrator Kafka consumer wiring (OMN-10535)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_session_orchestrator.consumer import (
    _DEFAULT_GROUP,
    TOPIC_SESSION_ORCH_COMPLETED,
    TOPIC_SESSION_ORCH_FAILED,
    TOPIC_SESSION_ORCH_START,
    _build_failure_payload,
    _parse_command,
)


@pytest.mark.unit
class TestSessionOrchestratorConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_start_topic(self) -> None:
        assert (
            TOPIC_SESSION_ORCH_START
            == "onex.cmd.omnimarket.session-orchestrator-start.v1"
        )

    def test_completed_topic(self) -> None:
        assert (
            TOPIC_SESSION_ORCH_COMPLETED
            == "onex.evt.omnimarket.session-orchestrator-completed.v1"
        )

    def test_failed_topic(self) -> None:
        assert (
            TOPIC_SESSION_ORCH_FAILED
            == "onex.evt.omnimarket.session-orchestrator-failed.v1"
        )

    def test_default_consumer_group(self) -> None:
        assert _DEFAULT_GROUP == "local.omnimarket.session_orchestrator.consume.1.0.0"


@pytest.mark.unit
class TestSessionOrchestratorParseCommand:
    """_parse_command extracts and defaults fields from raw Kafka payloads."""

    def test_defaults_when_empty_payload(self) -> None:
        cmd = _parse_command({})
        assert cmd["mode"] == "interactive"
        assert cmd["dry_run"] is False
        assert cmd["skip_health"] is False
        assert cmd["phase"] == 0
        assert isinstance(cmd["correlation_id"], str)
        assert len(cmd["correlation_id"]) > 0

    def test_explicit_values_override_defaults(self) -> None:
        cmd = _parse_command(
            {
                "correlation_id": "sess-abc-123",
                "session_id": "sess-2026-05-05-0900",
                "mode": "autonomous",
                "dry_run": True,
                "skip_health": True,
                "phase": 2,
            }
        )
        assert cmd["correlation_id"] == "sess-abc-123"
        assert cmd["session_id"] == "sess-2026-05-05-0900"
        assert cmd["mode"] == "autonomous"
        assert cmd["dry_run"] is True
        assert cmd["skip_health"] is True
        assert cmd["phase"] == 2

    def test_correlation_id_generated_when_missing(self) -> None:
        cmd1 = _parse_command({})
        cmd2 = _parse_command({})
        assert cmd1["correlation_id"] != cmd2["correlation_id"]

    def test_phase_coerced_from_string(self) -> None:
        cmd = _parse_command({"phase": "1"})
        assert cmd["phase"] == 1

    def test_dry_run_coerced_from_int(self) -> None:
        cmd = _parse_command({"dry_run": 1})
        assert cmd["dry_run"] is True

    def test_failure_event_shape(self) -> None:
        failure = _build_failure_payload("test-corr-789", RuntimeError("session error"))
        assert failure["correlation_id"] == "test-corr-789"
        assert "error" in failure
        assert "phase" in failure
        assert "failed_at" in failure
        assert failure["phase"] == "session_orchestrator"
