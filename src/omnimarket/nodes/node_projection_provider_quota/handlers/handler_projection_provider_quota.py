# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold: no clock, database, broker or envelope (OMN-20154)."""

from omnimarket.events.provider_quota import (
    PROVIDER_WIDE_MODEL_SCOPE,
    EnumProviderQuotaOutcome,
)
from omnimarket.nodes.node_projection_provider_quota.models import (
    ModelProviderQuotaProjectionRequest,
    ModelProviderQuotaProjectionResult,
    ModelProviderQuotaRowDelta,
)


class HandlerProjectionProviderQuota:
    """Derive the model-row and provider-row deltas of one observation.

    Every call counts on both rows: the model row answers "how hard are we
    driving this model" and the provider row "how hard are we driving this
    credential at this provider", which is the counter a pooled plan (the z.ai
    Coding Plan) actually enforces. A LIMIT_HIT sets its block on exactly the
    row its policy scope names. A CALL_OK clears, on both rows, any block
    recorded before the call was sent.
    """

    def handle(
        self, request: ModelProviderQuotaProjectionRequest
    ) -> ModelProviderQuotaProjectionResult:
        is_hit = request.outcome is EnumProviderQuotaOutcome.LIMIT_HIT
        clears = (
            request.call_started_at
            if request.outcome is EnumProviderQuotaOutcome.CALL_OK
            else None
        )
        rows = []
        for model_scope, scope_name in (
            (request.model_name, "model"),
            (PROVIDER_WIDE_MODEL_SCOPE, "provider"),
        ):
            sets_block = is_hit and request.block_scope == scope_name
            rows.append(
                ModelProviderQuotaRowDelta(
                    tenant_id=request.tenant_id,
                    credential_ref=request.credential_ref,
                    provider_id=request.provider_id,
                    model_scope=model_scope,
                    observed_at=request.observed_at,
                    window_seconds=request.window_seconds,
                    outcome=request.outcome,
                    http_status=request.http_status,
                    provider_code=request.provider_code,
                    counts_hit=is_hit,
                    sets_block=sets_block,
                    disposition=request.disposition if sets_block else None,
                    blocked_until=request.blocked_until if sets_block else None,
                    blocked_indefinitely=sets_block and request.blocked_indefinitely,
                    block_reason=request.reason if sets_block else None,
                    clears_blocks_before=clears,
                )
            )
        return ModelProviderQuotaProjectionResult(rows=tuple(rows))
