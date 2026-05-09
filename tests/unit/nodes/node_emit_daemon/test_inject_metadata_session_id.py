# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for _inject_metadata session_id fallback via CLAUDE_CODE_SESSION_ID (OMN-10753)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_inject_metadata_preserves_payload_session_id() -> None:
    """session_id in payload is preserved; env var is not used."""
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    server = EmitSocketServer(
        socket_path="/tmp/test.sock",
        queue=MagicMock(),
        registry=MagicMock(),
    )

    payload: dict[str, object] = {"session_id": "payload-session-id"}
    with patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "env-session-id"}):
        result = server._inject_metadata(payload, correlation_id=None)

    assert result["session_id"] == "payload-session-id"


@pytest.mark.unit
def test_inject_metadata_falls_back_to_env_var_when_session_id_absent() -> None:
    """When payload has no session_id, CLAUDE_CODE_SESSION_ID is injected."""
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    server = EmitSocketServer(
        socket_path="/tmp/test.sock",
        queue=MagicMock(),
        registry=MagicMock(),
    )

    expected_session_id = "55f47499-eec1-4906-9fe7-c1ca86e4e459"
    payload: dict[str, object] = {}
    with patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": expected_session_id}):
        result = server._inject_metadata(payload, correlation_id=None)

    assert result["session_id"] == expected_session_id


@pytest.mark.unit
def test_inject_metadata_falls_back_to_env_var_when_session_id_empty() -> None:
    """When payload has empty session_id, CLAUDE_CODE_SESSION_ID is injected."""
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    server = EmitSocketServer(
        socket_path="/tmp/test.sock",
        queue=MagicMock(),
        registry=MagicMock(),
    )

    expected_session_id = "55f47499-eec1-4906-9fe7-c1ca86e4e459"
    payload: dict[str, object] = {"session_id": ""}
    with patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": expected_session_id}):
        result = server._inject_metadata(payload, correlation_id=None)

    assert result["session_id"] == expected_session_id


@pytest.mark.unit
def test_inject_metadata_no_session_id_when_env_unset() -> None:
    """When neither payload nor env var has session_id, no session_id is injected."""
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    server = EmitSocketServer(
        socket_path="/tmp/test.sock",
        queue=MagicMock(),
        registry=MagicMock(),
    )

    payload: dict[str, object] = {}
    env_without_session = {
        k: v
        for k, v in __import__("os").environ.items()
        if k != "CLAUDE_CODE_SESSION_ID"
    }
    with patch.dict("os.environ", env_without_session, clear=True):
        result = server._inject_metadata(payload, correlation_id=None)

    assert "session_id" not in result


@pytest.mark.unit
def test_inject_metadata_entity_id_derived_from_env_session_id() -> None:
    """entity_id is derived from the injected session_id UUID."""
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    server = EmitSocketServer(
        socket_path="/tmp/test.sock",
        queue=MagicMock(),
        registry=MagicMock(),
    )

    session_id = "55f47499-eec1-4906-9fe7-c1ca86e4e459"
    payload: dict[str, object] = {}
    with patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": session_id}):
        result = server._inject_metadata(payload, correlation_id=None)

    assert result["session_id"] == session_id
    assert result["entity_id"] == session_id
