# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI injection-path tests for OMN-10751.

Verifies that HandlerOverseerVerifierConsumer accepts an injected verifier
and uses it instead of constructing one internally.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier_consumer import (
    HandlerOverseerVerifierConsumer,
)

_BUS = cast(ProtocolEventBusPublisher, EventBusInmemory())


def _make_cmd(**overrides: object) -> bytes:
    defaults: dict[str, object] = {
        "correlation_id": "corr-di-test",
        "task_id": "OMN-9999",
        "status": "running",
        "domain": "build_loop",
        "node_id": "node_build_loop_orchestrator",
        "attempt": 1,
        "confidence": 0.9,
        "cost_so_far": 0.01,
        "allowed_actions": ["dispatch", "complete"],
        "schema_version": "1.0",
    }
    defaults.update(overrides)
    return json.dumps(defaults).encode()


@pytest.mark.unit
def test_consumer_accepts_injected_verifier() -> None:
    """Injected verifier is used; no internal construction."""
    stub_result: dict[str, Any] = {
        "verdict": "PASS",
        "failure_class": None,
        "summary": "stub pass",
        "checks": [{"name": "stub_check", "passed": True}],
    }
    mock_verifier = MagicMock()
    mock_verifier.verify.return_value = stub_result

    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS, verifier=mock_verifier)
    result = json.loads(consumer.process(_make_cmd()))

    assert mock_verifier.verify.called, "Injected verifier.verify() must be called"
    assert result["verdict"] == "PASS"
    assert result["passed"] is True


@pytest.mark.unit
def test_consumer_default_verifier_still_works() -> None:
    """Without injection, default HandlerOverseerVerifier is constructed and used."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(_make_cmd()))
    assert result["verdict"] in ("PASS", "FAIL", "ESCALATE")
