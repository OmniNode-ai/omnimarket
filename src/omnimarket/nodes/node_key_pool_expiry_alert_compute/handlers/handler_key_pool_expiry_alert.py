# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One alert for a tenant's key pool before its keys lapse (OMN-20993).

Pure: a tenant's catalog rows and a clock in, at most one alert out. The
verdict follows the same liveness rule the router applies when it picks a key
(``omnimarket.routing.tenant_overlay_resolver``): a key is live when it is not
revoked and its expiry is unset or still ahead of the clock.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

from omnimarket.nodes.node_key_pool_expiry_alert_compute.models import (
    ModelKeyPoolAlertKey,
    ModelKeyPoolCredentialRow,
    ModelKeyPoolExpiryAlert,
    ModelKeyPoolExpiryAlertRequest,
    ModelKeyPoolExpiryAlertResult,
    ModelKeyPoolExpirySlackCommand,
)


def _render_text(
    *,
    tenant_id: str,
    window_days: int,
    expiring: tuple[ModelKeyPoolAlertKey, ...],
    undated: tuple[ModelKeyPoolAlertKey, ...],
) -> str:
    lines = [
        f":warning: *Key pool expiry* — tenant `{tenant_id}`: "
        f"{len(expiring)} key(s) expire within {window_days} days"
    ]
    lines.extend(
        f"• `{key.name}` (`{key.api_key_ref}`) expires "
        f"{key.expires_at.date().isoformat()} (in {key.days_remaining} days)"
        for key in expiring
        if key.expires_at is not None
    )
    if undated:
        lines.append("Undated keys in this pool (no expiry set):")
        lines.extend(f"• `{key.name}` (`{key.api_key_ref}`)" for key in undated)
    return "\n".join(lines)


def _idempotency_key(tenant_id: str, window_days: int, refs: tuple[str, ...]) -> str:
    """Stable for one set of expiring keys; a new member makes a new alert."""
    # These are public references, not credential values. Encode the complete
    # identity without hashing; JSON preserves field and reference boundaries.
    identity = json.dumps(
        [tenant_id, window_days, sorted(set(refs))], separators=(",", ":")
    )
    token = base64.urlsafe_b64encode(identity.encode()).decode("ascii")
    return f"key-pool-expiry|{token}"


class HandlerKeyPoolExpiryAlert:
    """Decide whether a tenant's key pool warrants its one expiry alert."""

    def handle(
        self, request: ModelKeyPoolExpiryAlertRequest
    ) -> ModelKeyPoolExpiryAlertResult:
        now = request.now
        horizon = now + timedelta(days=request.warning_window_days)

        live: list[ModelKeyPoolCredentialRow] = [
            row
            for row in request.credentials
            if row.revoked_at is None
            and (row.expires_at is None or row.expires_at > now)
        ]
        expiring_rows = sorted(
            (
                row
                for row in live
                if row.expires_at is not None and row.expires_at <= horizon
            ),
            key=lambda row: (row.expires_at, row.api_key_ref),
        )
        if not expiring_rows:
            return ModelKeyPoolExpiryAlertResult(tenant_id=request.tenant_id)

        expiring = tuple(
            ModelKeyPoolAlertKey(
                name=row.name,
                api_key_ref=row.api_key_ref,
                expires_at=row.expires_at,
                days_remaining=(row.expires_at - now).days if row.expires_at else None,
            )
            for row in expiring_rows
        )
        undated_by_ref = {
            row.api_key_ref: ModelKeyPoolAlertKey(
                name=row.name, api_key_ref=row.api_key_ref
            )
            for row in sorted(live, key=lambda row: row.api_key_ref)
            if row.expires_at is None
        }
        undated = tuple(undated_by_ref.values())

        command = ModelKeyPoolExpirySlackCommand(
            channel=request.channel,
            text=_render_text(
                tenant_id=request.tenant_id,
                window_days=request.warning_window_days,
                expiring=expiring,
                undated=undated,
            ),
            idempotency_key=_idempotency_key(
                request.tenant_id,
                request.warning_window_days,
                tuple(key.api_key_ref for key in expiring),
            ),
            correlation_id=request.correlation_id,
        )
        return ModelKeyPoolExpiryAlertResult(
            tenant_id=request.tenant_id,
            alert=ModelKeyPoolExpiryAlert(
                tenant_id=request.tenant_id,
                window_days=request.warning_window_days,
                expiring=expiring,
                undated=undated,
                slack_command=command,
            ),
        )


__all__ = ["HandlerKeyPoolExpiryAlert"]
