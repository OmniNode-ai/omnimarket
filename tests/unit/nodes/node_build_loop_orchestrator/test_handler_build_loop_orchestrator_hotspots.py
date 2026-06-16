# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused unit tests for HandlerBuildLoopOrchestrator and adapter_llm_dispatch hotspots.

Covers:
- HandlerBuildLoopOrchestrator: zero-arg construction, protocol-based sub-handler
  injection, _single_topic matching, _load_topic_bindings contract loading
- adapter_llm_dispatch: _get_state_dir (env vs cwd), _load_topic_from_contract
  (found vs not found), _write_trace (creates dir + file, never raises on error)

These tests preserve orchestration behavior via structural assertions, not
end-to-end dispatch (which requires live Kafka + sub-node processes).

Related: OMN-12383
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch import (
    _get_state_dir,
    _load_topic_from_contract,
    _write_trace,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
    _single_topic,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelDispatchQualityGateResult,
    ModelDispatchTrace,
    ModelReviewResult,
)

# ---------------------------------------------------------------------------
# _single_topic (used in _load_topic_bindings contract loading)
# ---------------------------------------------------------------------------


class TestSingleTopic:
    def test_exact_fragment_match_returns_topic(self) -> None:
        topics = (
            "onex.evt.omnimarket.build-loop-orchestrator-start.v1",
            "onex.evt.omnimarket.build-loop-orchestrator-completed.v1",
        )
        result = _single_topic(
            topics,
            "build-loop-orchestrator-start",
            contract_path=Path("/fake/contract.yaml"),
            section="subscribe_topics",
        )
        assert result == "onex.evt.omnimarket.build-loop-orchestrator-start.v1"

    def test_no_match_raises_value_error(self) -> None:
        topics = ("onex.evt.omnimarket.other.v1",)
        with pytest.raises(ValueError, match="build-loop-orchestrator-start"):
            _single_topic(
                topics,
                "build-loop-orchestrator-start",
                contract_path=Path("/fake/contract.yaml"),
                section="subscribe_topics",
            )

    def test_multiple_matches_raises_value_error(self) -> None:
        topics = (
            "onex.evt.omnimarket.build-loop-orchestrator-start.v1",
            "onex.evt.omnimarket.build-loop-orchestrator-start-legacy.v1",
        )
        with pytest.raises(ValueError, match="expected exactly one"):
            _single_topic(
                topics,
                "build-loop-orchestrator-start",
                contract_path=Path("/fake/contract.yaml"),
                section="subscribe_topics",
            )


# ---------------------------------------------------------------------------
# HandlerBuildLoopOrchestrator: construction and protocol injection
# ---------------------------------------------------------------------------


class TestHandlerBuildLoopOrchestratorConstruction:
    def test_construction_with_event_bus_succeeds(self) -> None:
        """Construction with required event_bus does not raise."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        handler = HandlerBuildLoopOrchestrator(event_bus=mock_bus)
        assert handler is not None

    def test_explicit_sub_handler_injection(self) -> None:
        """Explicitly passed sub-handlers are stored on the instance."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        mock_closeout = MagicMock()
        mock_verify = MagicMock()
        handler = HandlerBuildLoopOrchestrator(
            event_bus=mock_bus,
            closeout=mock_closeout,
            verify=mock_verify,
        )
        assert handler._closeout is mock_closeout
        assert handler._verify is mock_verify

    def test_sub_handlers_default_to_none(self) -> None:
        """Optional sub-handlers default to None at construction time."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        handler = HandlerBuildLoopOrchestrator(event_bus=mock_bus)
        assert handler._closeout is None
        assert handler._verify is None

    def test_event_bus_stored_on_instance(self) -> None:
        """event_bus is stored and accessible on the handler."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        handler = HandlerBuildLoopOrchestrator(event_bus=mock_bus)
        assert handler._event_bus is mock_bus


# ---------------------------------------------------------------------------
# adapter_llm_dispatch: _get_state_dir
# ---------------------------------------------------------------------------


class TestGetStateDir:
    def test_uses_omni_home_env_when_set(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"OMNI_HOME": str(tmp_path)}, clear=False):
            result = _get_state_dir()
        assert result == tmp_path / ".onex_state"

    def test_falls_back_to_cwd_when_no_omni_home(self, tmp_path: Path) -> None:
        import os

        env = dict(os.environ.items())
        env.pop("OMNI_HOME", None)
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch.Path.cwd",
                return_value=tmp_path,
            ),
        ):
            result = _get_state_dir()
        assert result == tmp_path / ".onex_state"


# ---------------------------------------------------------------------------
# adapter_llm_dispatch: _load_topic_from_contract
# ---------------------------------------------------------------------------


class TestLoadTopicFromContract:
    def test_returns_matching_topic(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.yaml"
        contract.write_text(
            yaml.dump(
                {
                    "event_bus": {
                        "publish_topics": [
                            "onex.evt.omnimarket.delegation-attempt.v1",
                            "onex.evt.omnimarket.delegation-metrics.v1",
                        ]
                    }
                }
            )
        )
        result = _load_topic_from_contract(contract, "delegation-attempt")
        assert result == "onex.evt.omnimarket.delegation-attempt.v1"

    def test_raises_when_fragment_not_found(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.yaml"
        contract.write_text(
            yaml.dump({"event_bus": {"publish_topics": ["onex.evt.other.v1"]}})
        )
        with pytest.raises(ValueError, match="delegation-attempt"):
            _load_topic_from_contract(contract, "delegation-attempt")

    def test_raises_when_contract_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(ValueError, match="any-fragment"):
            _load_topic_from_contract(missing, "any-fragment")

    def test_raises_when_no_publish_topics_key(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.yaml"
        contract.write_text(yaml.dump({"event_bus": {}}))
        with pytest.raises(ValueError, match="delegation-attempt"):
            _load_topic_from_contract(contract, "delegation-attempt")


# ---------------------------------------------------------------------------
# adapter_llm_dispatch: _write_trace
# ---------------------------------------------------------------------------


def _make_trace(
    correlation_id: str = "test-corr-id",
    ticket_id: str = "OMN-9999",
    attempt: int = 1,
) -> ModelDispatchTrace:
    return ModelDispatchTrace(
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        attempt=attempt,
        timestamp=datetime.now(UTC).isoformat(),
        coder_model="qwen3-6b",
        prompt_tokens=100,
        completion_tokens=200,
        quality_gate=ModelDispatchQualityGateResult(
            ruff_pass=True,
            import_pass=True,
            test_pass=True,
            errors=[],
        ),
        review_result=ModelReviewResult(
            approved=True,
            issues=[],
            risk_level="low",
            reviewer_model="glm-reviewer",
            review_tokens=50,
        ),
        accepted=True,
    )


class TestWriteTrace:
    def test_creates_dir_and_writes_json_file(self, tmp_path: Path) -> None:
        trace = _make_trace()
        _write_trace(trace, tmp_path)
        traces_dir = tmp_path / "dispatch-traces"
        assert traces_dir.exists()
        files = list(traces_dir.glob("*.json"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "test-corr-id" in content
        assert "OMN-9999" in content

    def test_filename_includes_ticket_and_attempt(self, tmp_path: Path) -> None:
        trace = _make_trace(ticket_id="OMN-1234", attempt=3)
        _write_trace(trace, tmp_path)
        traces_dir = tmp_path / "dispatch-traces"
        files = list(traces_dir.glob("*OMN-1234*attempt-3*"))
        assert len(files) == 1

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """_write_trace must never propagate exceptions (fire-and-forget logging)."""
        trace = _make_trace()
        # Pass a read-only dir equivalent by patching mkdir to raise
        with patch(
            "omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch.Path.mkdir",
            side_effect=PermissionError("read-only"),
        ):
            # Must not raise
            _write_trace(trace, tmp_path)
