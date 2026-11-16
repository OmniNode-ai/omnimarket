# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's saved rate rows become the verdict compute's readings (OMN-23607).

A seam: what the runner wrote as strings (empty when unread) become the typed,
optional instants the pure handler judges by. A label's limited mark is the
latest one the pool's status mirror still stands behind (a mark on a revoked
token counts for nothing), taken as the max parsed value with no clock here:
the handler compares it with now.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolLabelReading,
)


def _moment(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest_limited_until(
    tokens: Sequence[ModelLimitToken], label: str
) -> datetime | None:
    moments = [
        _moment(token.limited_until)
        for token in tokens
        if token.label == label and not token.revoked
    ]
    marks = [moment for moment in moments if moment is not None]
    return max(marks) if marks else None


def _revoked_everywhere(tokens: Sequence[ModelLimitToken], label: str) -> bool:
    """A label is revoked only when every pool token of it is: one host's revoked login is not the account's."""
    held = [token for token in tokens if token.label == label]
    return bool(held) and all(token.revoked for token in held)


def readings_from_floor(
    accounts: Sequence[ModelFloorCloudAccount],
    pools: Sequence[ModelLimitTokenPool],
) -> tuple[ModelPoolLabelReading, ...]:
    """One reading per account: its label is the account's name."""
    tokens: tuple[ModelLimitToken, ...] = tuple(
        token for pool in pools for token in pool.tokens
    )
    return tuple(
        ModelPoolLabelReading(
            label=account.name,
            read_at=_moment(account.rate_read_at),
            five_hour_utilization=account.five_hour_utilization,
            five_hour_resets_at=_moment(account.five_hour_resets_at),
            seven_day_utilization=account.seven_day_utilization,
            seven_day_resets_at=_moment(account.seven_day_resets_at),
            limited_until=_latest_limited_until(tokens, account.name),
            revoked=_revoked_everywhere(tokens, account.name),
        )
        for account in accounts
    )


__all__ = ["readings_from_floor"]
