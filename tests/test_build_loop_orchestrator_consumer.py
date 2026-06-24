# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_build_loop_orchestrator Kafka consumer wiring (OMN-10465)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_build_loop_orchestrator import (
    consumer as build_loop_consumer,
)
from omnimarket.nodes.node_build_loop_orchestrator.consumer import (
    _DEFAULT_GROUP,
    _GROUP_CONTRACT_REF,
    TOPIC_BUILD_LOOP_COMPLETED,
    TOPIC_BUILD_LOOP_FAILED,
    TOPIC_BUILD_LOOP_START,
    _build_failure_payload,
    _parse_command,
    _resolve_group_id,
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
        """Verify the consumer failure payload helper has the required fields."""
        correlation_id = "test-corr-456"

        failure = _build_failure_payload(correlation_id, RuntimeError("boom"))

        assert failure["correlation_id"] == correlation_id
        assert "error" in failure
        assert "phase" in failure
        assert "failed_at" in failure


# ---------------------------------------------------------------------------
# OMN-13557 Wave-2 (config -> overlay): the BUILD_LOOP_GROUP consumer-group
# config read resolves through the sanctioned overlay seam
# (``expand_contract_env_refs``) against a ``${env.VAR}`` contract ref, not a
# scattered direct ``os.environ`` read. Same var, same value, now via the one
# env-reading surface. Resolution-equivalence + fail-to-default coverage. The
# consumer-group suffix legitimately carries a contract default (vs a
# fail-closed endpoint), so an unbound overlay var falls back to the canonical
# default constant rather than raising.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildLoopGroupOverlayResolution:
    """Overlay resolution equivalence for the build-loop consumer-group config."""

    def test_contract_ref_declares_env_overlay_convention(self) -> None:
        """The consumer declares a ``${env.VAR}`` contract ref for the group id."""
        assert _GROUP_CONTRACT_REF == "${env.BUILD_LOOP_GROUP}"

    def test_default_consumer_group_constant(self) -> None:
        assert (
            _DEFAULT_GROUP == "local.omnimarket.build_loop_orchestrator.consume.1.0.0"
        )

    def test_group_resolves_via_overlay_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_resolve_group_id routes through expand_contract_env_refs."""
        seen: list[str] = []
        real = build_loop_consumer.expand_contract_env_refs

        def _spy(value: str) -> str:
            seen.append(value)
            return real(value)

        monkeypatch.setattr(build_loop_consumer, "expand_contract_env_refs", _spy)
        monkeypatch.setenv(
            "BUILD_LOOP_GROUP",
            "stability.omnimarket.build_loop_orchestrator.consume.1.0.0",
        )
        assert (
            _resolve_group_id()
            == "stability.omnimarket.build_loop_orchestrator.consume.1.0.0"
        )
        assert "${env.BUILD_LOOP_GROUP}" in seen

    def test_unbound_overlay_falls_back_to_contract_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unbound overlay var resolves to the contract default constant."""
        monkeypatch.delenv("BUILD_LOOP_GROUP", raising=False)
        assert _resolve_group_id() == _DEFAULT_GROUP
