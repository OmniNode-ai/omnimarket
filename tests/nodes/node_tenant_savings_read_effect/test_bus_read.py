# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus read tests for the tenant savings read node."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.handlers.tenant_projection_read.timestamps import (
    NOT_CONFIGURED_DETAIL,
    UNAVAILABLE_DETAIL,
)
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
    ModelTenantReadFailed,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read_bus import (
    HandlerTenantSavingsReadBus,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
    ProtocolTenantDelegationProjectionReader,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
CORRELATION = UUID("22222222-2222-2222-2222-222222222222")


class InMemoryTenantProjectionReader(ProtocolTenantDelegationProjectionReader):
    """A reader that answers from in-memory rows, for the bus tests."""

    def __init__(
        self,
        savings: dict[UUID, ModelTenantSavingsAggregate] | None = None,
        runs: dict[UUID, tuple[ModelDelegationRunRow, ...]] | None = None,
        missing: bool = False,
        not_configured: bool = False,
        unavailable: bool = False,
    ) -> None:
        self._savings = savings or {}
        self._runs = runs or {}
        self._missing = missing
        self._not_configured = not_configured
        self._unavailable = unavailable

    async def read_savings(self, tenant_id: UUID) -> ModelTenantSavingsAggregate:
        if self._not_configured:
            raise ProjectionNotConfiguredError("not configured")
        if self._unavailable:
            raise ProjectionUnavailableError("unavailable")
        if self._missing:
            raise ProjectionMissingError("missing")
        row = self._savings.get(tenant_id)
        if row is None:
            raise ProjectionMissingError("missing")
        return row

    async def read_recent_delegations(
        self, tenant_id: UUID, limit: int
    ) -> tuple[ModelDelegationRunRow, ...]:
        if self._not_configured:
            raise ProjectionNotConfiguredError("not configured")
        if self._unavailable:
            raise ProjectionUnavailableError("unavailable")
        if self._missing:
            raise ProjectionMissingError("missing")
        rows = self._runs.get(tenant_id, ())
        return rows[:limit]


def _command(kind: EnumTenantReadKind) -> ModelTenantReadCommand:
    return ModelTenantReadCommand(
        correlation_id=CORRELATION,
        tenant_id=TENANT,
        kind=kind,
        limit=5,
    )


async def test_golden_chain_completed_echoes_identity_and_body() -> None:
    reader = InMemoryTenantProjectionReader(
        savings={
            TENANT: ModelTenantSavingsAggregate(
                total_saved_usd=4.0,
                total_spend_usd=2.0,
                delegation_runs=3,
                total_tokens=1200,
            )
        }
    )
    handler = HandlerTenantSavingsReadBus(reader=reader)
    command = _command(EnumTenantReadKind.SAVINGS)
    result = await handler.handle(command)
    assert isinstance(result, ModelTenantReadCompleted)
    assert result.correlation_id == CORRELATION
    assert result.tenant_id == TENANT
    assert result.kind == EnumTenantReadKind.SAVINGS
    assert result.body["total_saved_usd"] == 4.0
    assert result.body["total_tokens"] == 1200
    assert result.body["delegation_runs"] == 3


async def test_wrong_kind_fails() -> None:
    handler = HandlerTenantSavingsReadBus(reader=InMemoryTenantProjectionReader())
    command = _command(EnumTenantReadKind.DELEGATIONS)
    result = await handler.handle(command)
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "wrong_kind"


async def test_not_configured_fails() -> None:
    handler = HandlerTenantSavingsReadBus(
        reader=InMemoryTenantProjectionReader(not_configured=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.SAVINGS))
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "not_configured"
    assert result.detail == NOT_CONFIGURED_DETAIL


async def test_unavailable_fails() -> None:
    handler = HandlerTenantSavingsReadBus(
        reader=InMemoryTenantProjectionReader(unavailable=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.SAVINGS))
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "unavailable"
    assert result.detail == UNAVAILABLE_DETAIL


async def test_missing_projection_answers_insufficient_data() -> None:
    handler = HandlerTenantSavingsReadBus(
        reader=InMemoryTenantProjectionReader(missing=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.SAVINGS))
    assert isinstance(result, ModelTenantReadCompleted)
    assert result.body["data_state"] == "insufficient_data"


def test_invalid_tenant_id_refused_by_model() -> None:
    with pytest.raises(ValidationError):
        ModelTenantReadCommand.model_validate(
            {
                "correlation_id": CORRELATION,
                "tenant_id": "not-a-uuid",
                "kind": EnumTenantReadKind.SAVINGS,
                "limit": 5,
            }
        )


async def test_failing_control_drops_total_tokens() -> None:
    class _BrokenBusHandler(HandlerTenantSavingsReadBus):
        async def handle(
            self, command: ModelTenantReadCommand
        ) -> ModelTenantReadCompleted | ModelTenantReadFailed:
            answer = await super().handle(command)
            if isinstance(answer, ModelTenantReadCompleted):
                body = dict(answer.body)
                body.pop("total_tokens", None)
                return ModelTenantReadCompleted(
                    correlation_id=command.correlation_id,
                    kind=EnumTenantReadKind.SAVINGS,
                    tenant_id=command.tenant_id,
                    body=body,
                )
            return answer

    reader = InMemoryTenantProjectionReader(
        savings={
            TENANT: ModelTenantSavingsAggregate(
                total_saved_usd=4.0,
                total_spend_usd=2.0,
                delegation_runs=3,
                total_tokens=1200,
            )
        }
    )
    broken = _BrokenBusHandler(reader=reader)
    command = _command(EnumTenantReadKind.SAVINGS)
    result = await broken.handle(command)
    assert isinstance(result, ModelTenantReadCompleted)
    assert "total_tokens" not in result.body
    with pytest.raises((AssertionError, KeyError)):
        assert result.body["total_tokens"] == 1200


def test_terminal_topics_of_the_contract_are_the_completed_and_failed_events() -> None:
    """The output states the bus handler answers with are the contract's two terminal topics."""
    from tests.nodes.tenant_read_support import contract_of

    dispatch = contract_of("node_tenant_savings_read_effect")["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.tenant-savings-read-requested.v1"
    )
    assert dispatch["terminal_events"] == {
        "success": "onex.evt.omnimarket.tenant-savings-read-completed.v1",
        "failure": "onex.evt.omnimarket.tenant-savings-read-failed.v1",
    }
