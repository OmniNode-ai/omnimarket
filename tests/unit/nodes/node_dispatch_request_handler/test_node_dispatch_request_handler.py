# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for NodeDispatchRequestHandler (OMN-12146)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request import (
    NodeDispatchRequestHandler,
)
from omnimarket.nodes.node_dispatch_request_handler.models.model_dispatch_request import (
    ModelDispatchRequest,
    ModelDispatchResult,
)

_KNOWN_NODES = frozenset({"node_build_loop", "node_session_orchestrator", "node_demo"})


def _make_request(**overrides: object) -> ModelDispatchRequest:
    defaults: dict[str, object] = {
        "request_id": "req-001",
        "command_type": "run-node",
        "target_node_id": "node_build_loop",
        "payload": {"ticket_id": "OMN-12146"},
        "requested_by": "dashboard",
        "requested_at": "2026-05-25T00:00:00Z",
    }
    defaults.update(overrides)
    return ModelDispatchRequest(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def handler() -> NodeDispatchRequestHandler:
    return NodeDispatchRequestHandler()


@pytest.fixture
def patch_known_nodes():
    with patch(
        "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request._known_node_ids",
        return_value=_KNOWN_NODES,
    ) as m:
        yield m


@pytest.mark.unit
class TestCommandTypeValidation:
    def test_rejects_unknown_command_type(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="explode")
        result = handler.handle(req)
        assert result.status == "rejected"
        assert "explode" in (result.error_message or "")
        assert result.request_id == "req-001"

    def test_rejects_empty_command_type(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="")
        result = handler.handle(req)
        assert result.status == "rejected"

    @pytest.mark.parametrize("cmd_type", ["run-node", "trigger-delegation", "cancel"])
    def test_accepts_all_supported_command_types(
        self,
        cmd_type: str,
        handler: NodeDispatchRequestHandler,
        patch_known_nodes: MagicMock,
    ) -> None:
        req = _make_request(command_type=cmd_type)
        with patch(
            "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = handler.handle(req)
        assert result.status == "dispatched"
        assert result.target_node_id == "node_build_loop"


@pytest.mark.unit
class TestTargetNodeValidation:
    def test_rejects_unknown_target_node(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(target_node_id="node_does_not_exist")
        result = handler.handle(req)
        assert result.status == "rejected"
        assert "node_does_not_exist" in (result.error_message or "")

    def test_accepts_known_target_node(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(target_node_id="node_demo", command_type="cancel")
        result = handler.handle(req)
        assert result.status == "dispatched"


@pytest.mark.unit
class TestRunNodeRouting:
    def test_run_node_dispatched_on_success(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="run-node")
        with patch(
            "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = handler.handle(req)
        assert result.status == "dispatched"
        assert result.error_message is None

    def test_run_node_fails_on_nonzero_exit(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="run-node")
        with patch(
            "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="boom")
            result = handler.handle(req)
        assert result.status == "failed"
        assert "boom" in (result.error_message or "")

    def test_run_node_fails_on_timeout(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        import subprocess as sp

        req = _make_request(command_type="run-node")
        with patch(
            "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="onex", timeout=25),
        ):
            result = handler.handle(req)
        assert result.status == "failed"
        assert "timed out" in (result.error_message or "")

    def test_run_node_fails_on_oserror(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="run-node")
        with patch(
            "omnimarket.nodes.node_dispatch_request_handler.handlers.handler_dispatch_request.subprocess.run",
            side_effect=OSError("not found"),
        ):
            result = handler.handle(req)
        assert result.status == "failed"
        assert "not found" in (result.error_message or "")


@pytest.mark.unit
class TestDelegationAndCancelRouting:
    def test_trigger_delegation_dispatched(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="trigger-delegation")
        result = handler.handle(req)
        assert result.status == "dispatched"
        assert result.error_message is None

    def test_cancel_dispatched(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(command_type="cancel")
        result = handler.handle(req)
        assert result.status == "dispatched"
        assert result.error_message is None


@pytest.mark.unit
class TestResultModel:
    def test_result_is_frozen(self) -> None:
        result = ModelDispatchResult(
            request_id="r",
            status="dispatched",
            target_node_id="node_x",
            dispatched_at="2026-05-25T00:00:00Z",
        )
        with pytest.raises((TypeError, ValidationError)):
            result.status = "mutated"  # type: ignore[misc]

    def test_result_preserves_request_id(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        req = _make_request(request_id="unique-id-999", command_type="cancel")
        result = handler.handle(req)
        assert result.request_id == "unique-id-999"

    def test_dispatched_at_is_iso8601(
        self, handler: NodeDispatchRequestHandler, patch_known_nodes: MagicMock
    ) -> None:
        from datetime import datetime

        req = _make_request(command_type="cancel")
        result = handler.handle(req)
        dt = datetime.fromisoformat(result.dispatched_at)
        assert dt.tzinfo is not None
