# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer a tenant savings bus command with a completed or failed terminal event.

The body is the HTTP route's body plus ``total_tokens``: the HTTP shape is held to the recorded legacy corpus
(the T18b replay), so the extra figure rides on the bus terminal only. Authority over who may ask for a tenant
is the broker access rule on the command topic, never a field of the command.
"""

from __future__ import annotations

from omnimarket.handlers.tenant_projection_read.analytics_postgres import (
    ProtocolLogicalSecretResolver,
)
from omnimarket.handlers.tenant_projection_read.resolver_reader import (
    reader_for_node,
)
from omnimarket.handlers.tenant_projection_read.timestamps import (
    NOT_CONFIGURED_DETAIL,
    UNAVAILABLE_DETAIL,
    SystemClock,
    iso_z,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
    ModelTenantReadFailed,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read import (
    CURRENCY,
)
from omnimarket.nodes.node_tenant_savings_read_effect.models.model_tenant_savings import (
    ModelTenantSavings,
)
from omnimarket.protocols.protocol_clock import ProtocolClock
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
    ProtocolTenantDelegationProjectionReader,
)

KIND = EnumTenantReadKind.SAVINGS


class HandlerTenantSavingsReadBus:
    """Never fabricates a figure: a missing projection is ``insufficient_data`` with zero totals."""

    def __init__(
        self,
        *,
        reader: ProtocolTenantDelegationProjectionReader | None = None,
        secret_resolver: ProtocolLogicalSecretResolver | None = None,
        clock: ProtocolClock | None = None,
    ) -> None:
        """Build the bus handler.

        Args:
            reader: A bound projection reader. The runtime hands the handler
                with no arguments, so with no ``reader`` the reader is built
                from a secret resolver.
            secret_resolver: The runtime hands it, or the handler reads the
                lane's rendered resolver config; with none the handler answers
                ``not_configured``.
            clock: The clock the completed body's ``as_of`` reads; defaults to
                the host clock.
        """
        self._reader = (
            reader
            if reader is not None
            else reader_for_node(
                "omnimarket.nodes.node_tenant_savings_read_effect", secret_resolver
            )
        )
        self._clock: ProtocolClock = clock or SystemClock()

    async def handle(
        self, request: ModelTenantReadCommand
    ) -> ModelTenantReadCompleted | ModelTenantReadFailed:
        if request.kind is not KIND:
            return self._failed(request, "wrong_kind", "command kind is not savings")
        if self._reader is None:
            return self._failed(request, "not_configured", NOT_CONFIGURED_DETAIL)
        try:
            row = await self._reader.read_savings(request.tenant_id)
        except ProjectionMissingError:
            return self._completed(
                request,
                ModelTenantSavings(
                    total_saved_usd=0.0,
                    total_spend_usd=0.0,
                    net_saved_usd=0.0,
                    delegation_runs=0,
                    currency=CURRENCY,
                    as_of=iso_z(self._clock.now()),
                    data_state="insufficient_data",
                ),
                total_tokens=0,
            )
        except ProjectionNotConfiguredError:
            return self._failed(request, "not_configured", NOT_CONFIGURED_DETAIL)
        except ProjectionUnavailableError:
            return self._failed(request, "unavailable", UNAVAILABLE_DETAIL)
        has_data = (
            row.delegation_runs > 0
            or row.total_saved_usd != 0.0
            or row.total_spend_usd != 0.0
            or row.total_tokens != 0
        )
        return self._completed(
            request,
            ModelTenantSavings(
                total_saved_usd=row.total_saved_usd,
                total_spend_usd=row.total_spend_usd,
                net_saved_usd=row.total_saved_usd,
                delegation_runs=row.delegation_runs,
                currency=CURRENCY,
                as_of=iso_z(row.savings_latest or row.runs_latest or self._clock.now()),
                data_state="ok" if has_data else "insufficient_data",
            ),
            total_tokens=row.total_tokens,
        )

    @staticmethod
    def _completed(
        request: ModelTenantReadCommand,
        figures: ModelTenantSavings,
        *,
        total_tokens: int,
    ) -> ModelTenantReadCompleted:
        return ModelTenantReadCompleted(
            correlation_id=request.correlation_id,
            kind=KIND,
            tenant_id=request.tenant_id,
            body={**figures.model_dump(), "total_tokens": total_tokens},
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
