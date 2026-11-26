# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bus request with no terminal names the exception class when the exception carries no message (OMN-24874).

``asyncio.TimeoutError`` raised bare has an empty ``str()``. An exception object is always truthy, so the reason
``f"{exc or type(exc).__name__}"`` rendered as the empty string and the row read ``no tenant read terminal: `` with
nothing after the colon. These tests hold both adapters (R8's tenant read and R9's corpus request) to a named cause.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
)
from omnimarket.models.scm import ModelScmTopics
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
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers import (
    adapter_r9_corpus,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.adapter_r9_corpus import (
    build_corpus_request,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
    load_bus_settings,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OVERLAY = ModelScenarioTenantReadOverlay(
    bus="inmemory", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)


class _RaisingCaller:
    """A bus caller whose call raises the exception the test hands the module, with no message."""

    error: BaseException = TimeoutError()

    def __init__(self, bus: object) -> None:
        self._bus = bus

    async def call_topics(
        self,
        *,
        topics: ModelScmTopics,
        request: object,
        correlation_id: UUID,
        timeout_s: float,
    ) -> object:
        raise self.error


@asynccontextmanager
async def _fake_open_bus(bus_opts: dict[str, object]) -> AsyncIterator[object]:
    yield object()


def _tenant_read_reason(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> str:
    _RaisingCaller.error = error
    monkeypatch.setattr(adapter_r8_bus_request, "_open_bus", _fake_open_bus)
    monkeypatch.setattr(adapter_r8_bus_request, "ScmBusCaller", _RaisingCaller)
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
    assert not answer.completed
    return answer.reason


def _corpus_reason(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> str:
    _RaisingCaller.error = error
    monkeypatch.setattr(adapter_r9_corpus, "_open_bus", _fake_open_bus)
    monkeypatch.setattr(adapter_r9_corpus, "ScmBusCaller", _RaisingCaller)
    settings = load_bus_settings()
    request = build_corpus_request(
        OVERLAY,
        command_node=settings.command_node,
        workspace_env=settings.workspace_env,
        environment={},
    )
    answer = request({"case": "empty-exception"}, 1.0)
    assert not answer.answered
    assert answer.reason is not None
    return answer.reason


def test_error_chain_a_bare_timeout_names_its_class_in_the_tenant_read_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = _tenant_read_reason(monkeypatch, TimeoutError())
    assert "TimeoutError" in reason
    assert not reason.endswith(": ")


def test_error_chain_a_bare_timeout_names_its_class_in_the_corpus_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = _corpus_reason(monkeypatch, TimeoutError())
    assert "TimeoutError" in reason
    assert not reason.endswith(": ")


def test_golden_an_exception_with_a_message_keeps_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _tenant_read_reason(monkeypatch, TimeoutError("bus closed"))
        == "no tenant read terminal: bus closed"
    )
    assert (
        _corpus_reason(monkeypatch, TimeoutError("bus closed"))
        == "no terminal: bus closed"
    )
