# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A harness pin reaches the harness handler even when the runtime injects its own dispatch port (OMN-19998).

Measured on the lab dev lane (2026-11-28): ``onex delegate --backend-id harness-claude-sonnet`` ended
``every_rung_failed`` on ``byok-anthropic`` (HTTP 401, tier tenant_overlay) because the deployed runtime
builds the handler with its own bus port, which has no harness route. Exemplar:
tests/unit/delegation/test_harness_execute_producer_omn26220.py (the producer on the Market port).
"""

from __future__ import annotations

import inspect
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_harness_pin_dispatch import (
    HarnessPinDispatchPort,
    with_harness_pin,
)
from tests.unit.delegation.harness_support_omn26219 import load_overlay
from tests.unit.delegation.test_harness_execute_producer_omn26220 import (
    HARNESS_BACKEND,
    HARNESS_COMMAND_TOPIC,
    LAB_BACKEND,
    LoopbackBus,
    accepted,
)

pytestmark = pytest.mark.unit


class InjectedBusPort:
    """Stands in for the runtime's own port: a bus, a ``dispatch`` and no harness route."""

    def __init__(self, bus: object | None) -> None:
        self._event_bus = bus
        self.calls: list[dict[str, Any]] = []

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: object,
        wait: bool,
        execution_timeout_seconds: int,
        tenant_id: str | None,
        backend_id: str | None = None,
        attribution: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append({"backend_id": backend_id, "prompt": prompt})
        return {"status": "completed", "content": "generic path"}


def _call(port: Any, backend_id: str | None) -> Any:
    return port.dispatch(
        prompt="Summarize the facts.",
        task_type="document",
        correlation_id=uuid4(),
        wait=True,
        execution_timeout_seconds=30,
        tenant_id="omninode",
        backend_id=backend_id,
        attribution={"caller_lane": "q-claudemed-220000"},
    )


async def test_a_harness_pin_on_the_injected_port_goes_to_the_harness_topic() -> None:
    bus = LoopbackBus(accepted)
    inner = InjectedBusPort(bus)
    port = HarnessPinDispatchPort(
        inner=inner, event_bus=bus, harness_overlay=load_overlay()
    )

    result = await _call(port, HARNESS_BACKEND)

    assert [topic for topic, _ in bus.published] == [HARNESS_COMMAND_TOPIC]
    assert inner.calls == []
    assert result["content"] == "the harness answer"


async def test_any_other_pin_and_no_pin_stay_on_the_injected_port() -> None:
    bus = LoopbackBus(accepted)
    inner = InjectedBusPort(bus)
    port = HarnessPinDispatchPort(
        inner=inner, event_bus=bus, harness_overlay=load_overlay()
    )

    first = await _call(port, LAB_BACKEND)
    second = await _call(port, None)

    assert bus.published == []
    assert [c["backend_id"] for c in inner.calls] == [LAB_BACKEND, None]
    assert first["content"] == second["content"] == "generic path"


def test_the_wrapper_declares_the_arguments_of_the_injected_port() -> None:
    inner = InjectedBusPort(LoopbackBus(None))
    port = with_harness_pin(inner)
    assert isinstance(port, HarnessPinDispatchPort)
    assert inspect.signature(port.dispatch) == inspect.signature(inner.dispatch)


def test_a_port_with_no_bus_is_left_as_it_is() -> None:
    inner = InjectedBusPort(None)
    assert with_harness_pin(inner) is inner


def test_the_handler_wraps_the_port_the_runtime_injects() -> None:
    inner = InjectedBusPort(LoopbackBus(None))
    handler = HandlerDelegateSkill(dispatch_port=cast("Any", inner))
    assert isinstance(handler._dispatch_port, HarnessPinDispatchPort)
