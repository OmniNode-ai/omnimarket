# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer a tenant's most recent delegation runs from the delegation projections."""

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

logger = logging.getLogger(__name__)


class HandlerTenantDelegationsRead:
    """Newest runs first, bounded by the request limit; never fabricates a run."""

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
            rows = await self._reader.read_recent_delegations(
                request.tenant_id, request.limit
            )
        except ProjectionMissingError:
            logger.info("recent delegations: primary projection absent; empty envelope")
            return self._ok(
                ModelRecentDelegations(
                    generated_at=iso_z(self._clock.now()),
                    count=0,
                    data_state="insufficient_data",
                    runs=[],
                )
            )
        except ProjectionNotConfiguredError:
            return self._failure(NOT_CONFIGURED_DETAIL)
        except ProjectionUnavailableError:
            return self._failure(UNAVAILABLE_DETAIL)
        now = self._clock.now()
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
        return self._ok(
            ModelRecentDelegations(
                generated_at=iso_z(now),
                count=len(runs),
                data_state="ok" if runs else "insufficient_data",
                runs=runs,
            )
        )

    @staticmethod
    def _ok(body: ModelRecentDelegations) -> ModelTenantReadHttpResponse:
        return ModelTenantReadHttpResponse(status_code=200, body=body.model_dump())

    @staticmethod
    def _failure(detail: str) -> ModelTenantReadHttpResponse:
        return ModelTenantReadHttpResponse(status_code=503, body={"detail": detail})
