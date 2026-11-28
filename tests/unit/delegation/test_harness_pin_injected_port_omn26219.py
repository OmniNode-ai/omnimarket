# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A harness pin reaches the harness handler when the runtime injects its own dispatch port (OMN-26219).

On the dev runtime the infra auto-wiring injects its own ``dispatch_port`` into the
delegate-skill handler, so the harness-aware port the handler would select for itself never
ran: a pin on a ``kind: harness`` backend went down the generic inference path (read on dev,
2026-11-28: the pinned run ended on the tenant-overlay rung with a 401). The handler now
sends a pin the routing overlay declares as a harness backend to the harness handler
whichever port was injected.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
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


class _InjectedPort:
    """The runtime's own port: records what reached it, answers like a lab rung."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "status": "completed",
            "content": "generic path answer",
            "model_name": "lab-model",
            "provider": LAB_BACKEND,
            "quality_gate_passed": True,
        }


def _handler(port: _InjectedPort, bus: LoopbackBus | None) -> HandlerDelegateSkill:
    return HandlerDelegateSkill(
        cast(Any, bus),
        dispatch_port=cast(ProtocolDelegationDispatchPort, port),
        harness_overlay=load_overlay(),
    )


def _request(backend_id: str | None) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest.model_validate(
        {
            "prompt": "Summarize the facts.",
            "task_type": "document",
            "source": "claude-code",
            "correlation_id": str(uuid4()),
            "tenant_id": "omninode",
            **({"backend_id": backend_id} if backend_id else {}),
        }
    )


async def test_harness_pin_bypasses_the_injected_port_and_reaches_the_harness_topic() -> (
    None
):
    port, bus = _InjectedPort(), LoopbackBus(accepted)

    terminal = await _handler(port, bus).handle(_request(HARNESS_BACKEND))

    assert [topic for topic, _ in bus.published] == [HARNESS_COMMAND_TOPIC]
    assert port.calls == []
    assert terminal.status == "completed"


async def test_a_non_harness_pin_still_goes_to_the_injected_port() -> None:
    port, bus = _InjectedPort(), LoopbackBus(accepted)

    await _handler(port, bus).handle(_request(LAB_BACKEND))

    assert bus.published == []
    assert len(port.calls) == 1


async def test_no_pin_goes_to_the_injected_port() -> None:
    port, bus = _InjectedPort(), LoopbackBus(accepted)

    await _handler(port, bus).handle(_request(None))

    assert bus.published == []
    assert len(port.calls) == 1


async def test_a_harness_pin_without_a_bus_stays_on_the_injected_port() -> None:
    """Control: nothing to publish on, so the handler does not invent a path."""
    port = _InjectedPort()

    await _handler(port, None).handle(_request(HARNESS_BACKEND))

    assert len(port.calls) == 1
