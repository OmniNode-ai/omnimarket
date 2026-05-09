# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerCrossCliOriginator (OMN-10143)."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from omnimarket.nodes.node_cross_cli_originator.handlers.handler_cross_cli_originator import (
    _TOPIC_DELEGATION_CMD,
    HandlerCrossCliOriginator,
)
from omnimarket.nodes.node_cross_cli_originator.models.model_cross_cli_originator_input import (
    ModelCrossCliOriginatorInput,
)
from omnimarket.nodes.node_cross_cli_originator.models.model_cross_cli_originator_result import (
    ModelCrossCliOriginatorResult,
)


def _make_mock_client(event_id: str = "evt-abc-123") -> MagicMock:
    client = MagicMock()
    client.emit_sync.return_value = event_id
    return client


@pytest.mark.unit
class TestHandlerCrossCliOriginator:
    def test_publishes_envelope_and_returns_result(self) -> None:
        mock_client = _make_mock_client("evt-001")
        handler = HandlerCrossCliOriginator(emit_client=mock_client)
        command = ModelCrossCliOriginatorInput(
            prompt="Run OMN-9999", task_type="research"
        )

        result = handler.handle(command)

        assert isinstance(result, ModelCrossCliOriginatorResult)
        assert result.event_id == "evt-001"
        assert result.topic == _TOPIC_DELEGATION_CMD
        assert isinstance(result.correlation_id, UUID)

    def test_uses_provided_correlation_id(self) -> None:
        fixed_id = UUID("12345678-1234-5678-1234-567812345678")
        mock_client = _make_mock_client("evt-002")
        handler = HandlerCrossCliOriginator(emit_client=mock_client)
        command = ModelCrossCliOriginatorInput(
            prompt="Test delegation",
            correlation_id=fixed_id,
        )

        result = handler.handle(command)

        assert result.correlation_id == fixed_id

    def test_emit_sync_called_with_correct_event_type(self) -> None:
        mock_client = _make_mock_client("evt-003")
        handler = HandlerCrossCliOriginator(emit_client=mock_client)
        command = ModelCrossCliOriginatorInput(
            prompt="Deploy OMN-8000",
            task_type="deploy",
            session_id="session-xyz",
        )

        handler.handle(command)

        mock_client.emit_sync.assert_called_once()
        call_kwargs = mock_client.emit_sync.call_args
        assert (
            call_kwargs.kwargs["event_type"]
            == "omnimarket.cross-cli-delegation-requested"
        )
        payload = call_kwargs.kwargs["payload"]
        assert payload["prompt"] == "Deploy OMN-8000"
        assert payload["task_type"] == "deploy"
        assert payload["session_id"] == "session-xyz"
        assert payload["source"] == "cross_cli_originator"

    def test_client_closed_after_publish(self) -> None:
        mock_client = _make_mock_client()
        handler = HandlerCrossCliOriginator(emit_client=mock_client)
        command = ModelCrossCliOriginatorInput(prompt="Test close")

        handler.handle(command)

        mock_client.close.assert_called_once()

    def test_client_closed_even_on_emit_failure(self) -> None:
        mock_client = MagicMock()
        mock_client.emit_sync.side_effect = ValueError("daemon rejected")
        handler = HandlerCrossCliOriginator(emit_client=mock_client)
        command = ModelCrossCliOriginatorInput(prompt="Test failure path")

        with pytest.raises(ValueError, match="daemon rejected"):
            handler.handle(command)

        mock_client.close.assert_called_once()

    def test_contract_topic_loaded_from_yaml(self) -> None:
        assert "cross-cli-delegation-requested" in _TOPIC_DELEGATION_CMD
        assert _TOPIC_DELEGATION_CMD.startswith("onex.")

    def test_handler_type_and_category(self) -> None:
        handler = HandlerCrossCliOriginator(emit_client=_make_mock_client())
        assert handler.handler_type == "node_handler"
        assert handler.handler_category == "effect"
