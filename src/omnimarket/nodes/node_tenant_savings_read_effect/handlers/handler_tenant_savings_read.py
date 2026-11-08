# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer a tenant's savings headline from the savings and delegation projections."""

from __future__ import annotations

import logging

from omnimarket.handlers.tenant_projection_read.timestamps import (
    NOT_CONFIGURED_DETAIL,
    UNAVAILABLE_DETAIL,
    SystemClock,
    iso_z,
)
from omnimarket.models.model_tenant_delegation_projection import (
    ModelTenantReadHttpResponse,
    ModelTenantReadRequest,
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

logger = logging.getLogger(__name__)

CURRENCY = "USD"


class HandlerTenantSavingsRead:
    """Never fabricates a figure: a missing projection is ``insufficient_data``."""

    def __init__(
        self,
        *,
        reader: ProtocolTenantDelegationProjectionReader | None = None,
        clock: ProtocolClock | None = None,
    ) -> None:
        self._reader = reader
        self._clock: ProtocolClock = clock or SystemClock()

    async def handle(
        self, request: ModelTenantReadRequest
    ) -> ModelTenantReadHttpResponse:
        if self._reader is None:
            return self._failure(NOT_CONFIGURED_DETAIL)
        try:
            row = await self._reader.read_savings(request.tenant_id)
        except ProjectionMissingError:
            logger.info("tenant savings: projection tables absent; insufficient data")
            return self._ok(
                ModelTenantSavings(
                    total_saved_usd=0.0,
                    total_spend_usd=0.0,
                    net_saved_usd=0.0,
                    delegation_runs=0,
                    currency=CURRENCY,
                    as_of=iso_z(self._clock.now()),
                    data_state="insufficient_data",
                )
            )
        except ProjectionNotConfiguredError:
            return self._failure(NOT_CONFIGURED_DETAIL)
        except ProjectionUnavailableError:
            return self._failure(UNAVAILABLE_DETAIL)
        latest = row.savings_latest or row.runs_latest
        has_data = (
            row.delegation_runs > 0
            or row.total_saved_usd != 0.0
            or row.total_spend_usd != 0.0
        )
        return self._ok(
            ModelTenantSavings(
                total_saved_usd=row.total_saved_usd,
                total_spend_usd=row.total_spend_usd,
                # savings_usd is already net of the local cost.
                net_saved_usd=row.total_saved_usd,
                delegation_runs=row.delegation_runs,
                currency=CURRENCY,
                as_of=iso_z(latest or self._clock.now()),
                data_state="ok" if has_data else "insufficient_data",
            )
        )

    @staticmethod
    def _ok(body: ModelTenantSavings) -> ModelTenantReadHttpResponse:
        return ModelTenantReadHttpResponse(status_code=200, body=body.model_dump())

    @staticmethod
    def _failure(detail: str) -> ModelTenantReadHttpResponse:
        return ModelTenantReadHttpResponse(status_code=503, body={"detail": detail})
