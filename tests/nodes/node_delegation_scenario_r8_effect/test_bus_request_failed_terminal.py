# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""R8's bus request names the cause of every failed tenant read terminal (OMN-23822 follow-up).

A tenant read whose command reaches no dispatcher is answered by the runtime's boundary failure terminal, which carries
``failure_code`` and ``failure_reason`` and no ``reason`` or ``detail``. The request used to read only ``reason`` and
``detail`` and so reported ``None: None``; these tests hold the answer to a named cause for each failed shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    adapter_r8_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_bus_request import (
    build_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    load_lane_settings,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OVERLAY = ModelScenarioTenantReadOverlay(
    bus="inmemory", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)
#: The shape the runtime's boundary failure terminal carries on the dev runtime (measured 2026-11-12).
BOUNDARY_FAILURE: dict[str, JsonValue] = {
    "status": "failed",
    "failure_class": "HandlerDispatchFailureError",
    "failure_code": "ONEX_CORE_063_ITEM_NOT_REGISTERED",
    "retryable": False,
    "failure_reason": "no dispatcher registered for the command topic",
    "origin_topic": "onex.cmd.omnimarket.tenant-savings-read-requested.v1",
}
NODE_FAILURE: dict[str, JsonValue] = {
    "reason": "not_configured",
    "detail": "analytics source",
    "terminal_failure_cause": "not_configured",
}


def _answer(
    monkeypatch: pytest.MonkeyPatch, *, failed: bool, payload: Mapping[str, JsonValue]
) -> tuple[bool, str]:
    """Run the real bus request with a bus whose terminal is ``payload``; return (completed, reason)."""

    @asynccontextmanager
    async def fake_open_bus(bus_opts: dict[str, object]) -> AsyncIterator[object]:
        yield object()

    class _Caller:
        def __init__(self, bus: object) -> None:
            self._bus = bus

        async def call_topics(
            self,
            *,
            topics: ModelScmTopics,
            request: object,
            correlation_id: UUID,
            timeout_s: float,
        ) -> ModelScmTerminal:
            return ModelScmTerminal(
                receipt=ModelScmBusReceipt(
                    correlation_id=correlation_id,
                    command_topic=topics.command,
                    command_envelope_id=uuid4(),
                    terminal_topic=topics.failure if failed else topics.success,
                    terminal_envelope_id=uuid4(),
                ),
                failed=failed,
                payload=dict(payload),
            )

    monkeypatch.setattr(adapter_r8_bus_request, "_open_bus", fake_open_bus)
    monkeypatch.setattr(adapter_r8_bus_request, "ScmBusCaller", _Caller)
    settings = load_lane_settings(CONTRACT_PATH)
    assert settings.bus is not None
    request = build_bus_request(
        OVERLAY,
        savings_node=settings.bus.savings_node,
        delegations_node=settings.bus.delegations_node,
        workspace_env="WS",
        environment={},
    )
    answer = request(
        ModelTenantReadCommand(
            correlation_id=uuid4(), kind=EnumTenantReadKind.SAVINGS, tenant_id=TENANT
        ),
        1.0,
    )
    return answer.completed, answer.reason


def test_golden_the_node_failure_shape_reads_as_reason_and_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed, reason = _answer(monkeypatch, failed=True, payload=NODE_FAILURE)
    assert not completed
    assert reason == "not_configured: analytics source"


def test_error_chain_a_boundary_failure_terminal_names_its_code_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed, reason = _answer(monkeypatch, failed=True, payload=BOUNDARY_FAILURE)
    assert not completed
    assert "ONEX_CORE_063_ITEM_NOT_REGISTERED" in reason
    assert "no dispatcher registered for the command topic" in reason
    assert "None" not in reason


def test_error_chain_a_failed_terminal_with_no_cause_names_the_keys_it_did_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed, reason = _answer(
        monkeypatch, failed=True, payload={"status": "failed", "origin_topic": "t"}
    )
    assert not completed
    assert "None" not in reason
    assert "origin_topic" in reason
    assert "status" in reason


def test_failing_control_the_cause_comes_from_the_payload_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, first = _answer(monkeypatch, failed=True, payload=BOUNDARY_FAILURE)
    _, second = _answer(
        monkeypatch,
        failed=True,
        payload={**BOUNDARY_FAILURE, "failure_code": "ONEX_CORE_OTHER"},
    )
    assert first != second
    assert "ONEX_CORE_OTHER" in second
