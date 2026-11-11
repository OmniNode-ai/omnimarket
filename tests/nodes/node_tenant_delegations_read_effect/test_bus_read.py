# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus read tests for the tenant delegations read node."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.handlers.tenant_projection_read.timestamps import (
    NOT_CONFIGURED_DETAIL,
    UNAVAILABLE_DETAIL,
)
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
)
from omnimarket.models.model_tenant_read_bus import (
    TRAFFIC_CLASS_BY_DATA_SOURCE,
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
    ModelTenantReadFailed,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.handler_tenant_delegations_read_bus import (
    HandlerTenantDelegationsReadBus,
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
        runs: dict[UUID, tuple[ModelDelegationRunRow, ...]] | None = None,
        missing: bool = False,
        not_configured: bool = False,
        unavailable: bool = False,
    ) -> None:
        self._runs = runs or {}
        self._missing = missing
        self._not_configured = not_configured
        self._unavailable = unavailable

    async def read_savings(self, tenant_id: UUID) -> object:
        raise ProjectionMissingError("missing")

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


def _run_row() -> ModelDelegationRunRow:
    return ModelDelegationRunRow(
        run_id="run-a",
        task_type="t",
        model="m",
        quality_gate_passed=True,
        cost_usd=1.0,
        saved_usd=2.0,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        tokens=500,
        data_source="real",
    )


async def test_golden_chain_completed_echoes_identity_and_body() -> None:
    reader = InMemoryTenantProjectionReader(runs={TENANT: (_run_row(),)})
    handler = HandlerTenantDelegationsReadBus(reader=reader)
    command = _command(EnumTenantReadKind.DELEGATIONS)
    result = await handler.handle(command)
    assert isinstance(result, ModelTenantReadCompleted)
    assert result.correlation_id == CORRELATION
    assert result.tenant_id == TENANT
    assert result.kind == EnumTenantReadKind.DELEGATIONS
    run = result.body["runs"][0]
    assert run["run_id"] == "run-a"
    assert run["total_tokens"] == 500
    assert run["traffic_class"] == TRAFFIC_CLASS_BY_DATA_SOURCE["real"]


async def test_wrong_kind_fails() -> None:
    handler = HandlerTenantDelegationsReadBus(reader=InMemoryTenantProjectionReader())
    command = _command(EnumTenantReadKind.SAVINGS)
    result = await handler.handle(command)
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "wrong_kind"


async def test_not_configured_fails() -> None:
    handler = HandlerTenantDelegationsReadBus(
        reader=InMemoryTenantProjectionReader(not_configured=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.DELEGATIONS))
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "not_configured"
    assert result.detail == NOT_CONFIGURED_DETAIL


async def test_unavailable_fails() -> None:
    handler = HandlerTenantDelegationsReadBus(
        reader=InMemoryTenantProjectionReader(unavailable=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.DELEGATIONS))
    assert isinstance(result, ModelTenantReadFailed)
    assert result.reason == "unavailable"
    assert result.detail == UNAVAILABLE_DETAIL


async def test_missing_projection_answers_insufficient_data() -> None:
    handler = HandlerTenantDelegationsReadBus(
        reader=InMemoryTenantProjectionReader(missing=True)
    )
    result = await handler.handle(_command(EnumTenantReadKind.DELEGATIONS))
    assert isinstance(result, ModelTenantReadCompleted)
    assert result.body["data_state"] == "insufficient_data"


def test_invalid_tenant_id_refused_by_model() -> None:
    with pytest.raises(ValidationError):
        ModelTenantReadCommand(
            correlation_id=CORRELATION,
            tenant_id="not-a-uuid",  # type: ignore[arg-type]
            kind=EnumTenantReadKind.DELEGATIONS,
            limit=5,
        )


async def test_failing_control_drops_total_tokens() -> None:
    class _BrokenBusHandler(HandlerTenantDelegationsReadBus):
        async def handle(
            self, command: ModelTenantReadCommand
        ) -> ModelTenantReadCompleted | ModelTenantReadFailed:
            answer = await super().handle(command)
            if isinstance(answer, ModelTenantReadCompleted):
                body = dict(answer.body)
                runs = [dict(r) for r in body.get("runs", [])]
                for r in runs:
                    r.pop("total_tokens", None)
                body["runs"] = runs
                return ModelTenantReadCompleted(
                    correlation_id=command.correlation_id,
                    kind=EnumTenantReadKind.DELEGATIONS,
                    tenant_id=command.tenant_id,
                    body=body,
                )
            return answer

    reader = InMemoryTenantProjectionReader(runs={TENANT: (_run_row(),)})
    broken = _BrokenBusHandler(reader=reader)
    command = _command(EnumTenantReadKind.DELEGATIONS)
    result = await broken.handle(command)
    assert isinstance(result, ModelTenantReadCompleted)
    assert "total_tokens" not in result.body["runs"][0]
    with pytest.raises((AssertionError, KeyError)):
        assert result.body["runs"][0]["total_tokens"] == 500
