# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer a tenant delegations bus command with a completed or failed terminal event.

The body is the HTTP route's body with two extra keys per run, ``total_tokens`` and ``traffic_class`` (the
delegation projection's data_source column read through TRAFFIC_CLASS_BY_DATA_SOURCE): the HTTP shape is held to the
recorded legacy corpus (the T18b replay), so the extra figures ride on the bus terminal only. Authority over who may
ask for a tenant is the broker access rule on the command topic, never a field of the command.
"""

from __future__ import annotations

from omnimarket.handlers.tenant_projection_read.timestamps import (
    NOT_CONFIGURED_DETAIL,
    UNAVAILABLE_DETAIL,
    SystemClock,
    iso_z,
)
from omnimarket.models.model_tenant_read_bus import (
    TRAFFIC_CLASS_BY_DATA_SOURCE,
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
    ModelTenantReadFailed,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.models.model_recent_delegations import (
    ModelDelegationRun,
    ModelRecentDelegations,
)
from omnimarket.protocols.protocol_clock import ProtocolClock
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
    ProtocolTenantDelegationProjectionReader,
)

KIND = EnumTenantReadKind.DELEGATIONS


class HandlerTenantDelegationsReadBus:
    """Newest runs first, bounded by the command limit; never fabricates a run."""

    def __init__(
        self,
        *,
        reader: ProtocolTenantDelegationProjectionReader | None = None,
        clock: ProtocolClock | None = None,
    ) -> None:
        self._reader = reader
        self._clock: ProtocolClock = clock or SystemClock()

    async def handle(
        self, request: ModelTenantReadCommand
    ) -> ModelTenantReadCompleted | ModelTenantReadFailed:
        if request.kind is not KIND:
            return self._failed(
                request, "wrong_kind", "command kind is not delegations"
            )
        if self._reader is None:
            return self._failed(request, "not_configured", NOT_CONFIGURED_DETAIL)
        now = self._clock.now()
        try:
            rows = await self._reader.read_recent_delegations(
                request.tenant_id, request.limit
            )
        except ProjectionMissingError:
            return self._completed(
                request,
                ModelRecentDelegations(
                    generated_at=iso_z(now),
                    count=0,
                    data_state="insufficient_data",
                    runs=[],
                ),
                extras=[],
            )
        except ProjectionNotConfiguredError:
            return self._failed(request, "not_configured", NOT_CONFIGURED_DETAIL)
        except ProjectionUnavailableError:
            return self._failed(request, "unavailable", UNAVAILABLE_DETAIL)
        runs = [
            ModelDelegationRun(
                run_id=row.run_id,
                task_type=row.task_type,
                model=row.model,
                status="passed" if row.quality_gate_passed else "failed",
                cost_usd=row.cost_usd,
                saved_usd=row.saved_usd,
                occurred_at=iso_z(row.occurred_at or now),
            )
            for row in rows
        ]
        extras = [
            {
                "total_tokens": row.tokens,
                "traffic_class": TRAFFIC_CLASS_BY_DATA_SOURCE.get(
                    row.data_source, row.data_source
                ),
            }
            for row in rows
        ]
        return self._completed(
            request,
            ModelRecentDelegations(
                generated_at=iso_z(now),
                count=len(runs),
                data_state="ok" if runs else "insufficient_data",
                runs=runs,
            ),
            extras=extras,
        )

    @staticmethod
    def _completed(
        request: ModelTenantReadCommand,
        figures: ModelRecentDelegations,
        *,
        extras: list[dict[str, object]],
    ) -> ModelTenantReadCompleted:
        body = figures.model_dump()
        body["runs"] = [
            {**run, **extra} for run, extra in zip(body["runs"], extras, strict=True)
        ]
        return ModelTenantReadCompleted(
            correlation_id=request.correlation_id,
            kind=KIND,
            tenant_id=request.tenant_id,
            body=body,
        )

    @staticmethod
    def _failed(
        request: ModelTenantReadCommand, reason: str, detail: str
    ) -> ModelTenantReadFailed:
        return ModelTenantReadFailed(
            correlation_id=request.correlation_id,
            kind=request.kind,
            tenant_id=request.tenant_id,
            reason=reason,
            terminal_failure_cause=reason,
            detail=detail,
        )
