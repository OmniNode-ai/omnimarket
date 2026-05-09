# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for dispatch env propagation in node_session_orchestrator (OMN-9161)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    HandlerSessionOrchestrator,
)


@pytest.mark.unit
def test_dispatch_env_includes_session_and_run_ids() -> None:
    """_build_dispatch_env returns ONEX_SESSION_ID, ONEX_RUN_ID, ONEX_CORRELATION_PREFIX."""
    handler = HandlerSessionOrchestrator.__new__(HandlerSessionOrchestrator)

    env = handler._build_dispatch_env(
        session_id="sess-abc",
        dispatch_id="disp-001",
        ticket_id="OMN-1234",
    )

    assert env["ONEX_SESSION_ID"] == "sess-abc"
    assert env["ONEX_RUN_ID"] == "disp-001"
    assert env["ONEX_CORRELATION_PREFIX"] == "sess-abc.disp-001.OMN-1234"


@pytest.mark.unit
def test_dispatch_env_correlation_prefix_format() -> None:
    """ONEX_CORRELATION_PREFIX is dot-joined session.dispatch.ticket."""
    handler = HandlerSessionOrchestrator.__new__(HandlerSessionOrchestrator)

    env = handler._build_dispatch_env(
        session_id="session-xyz",
        dispatch_id="disp-003",
        ticket_id="OMN-9999",
    )

    assert env["ONEX_CORRELATION_PREFIX"] == "session-xyz.disp-003.OMN-9999"
    assert set(env.keys()) == {
        "ONEX_SESSION_ID",
        "ONEX_RUN_ID",
        "ONEX_CORRELATION_PREFIX",
    }
