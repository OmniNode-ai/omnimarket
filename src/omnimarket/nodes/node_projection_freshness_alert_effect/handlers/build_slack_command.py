# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a stale finding as one plain mrkdwn message."""

from uuid import UUID

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    ModelFreshnessCheck,
    ModelFreshnessSlackCommand,
    ModelFreshnessVerdict,
)

# Unit conversion, not an alert threshold.
_SECONDS_PER_MINUTE = 60


def build_slack_command(
    *,
    check: ModelFreshnessCheck,
    verdict: ModelFreshnessVerdict,
    channel: str,
    idempotency_key: str,
    correlation_id: UUID,
) -> ModelFreshnessSlackCommand:
    """All alert facts and correlation context travel on the existing Slack command."""
    newest = (
        verdict.supply_newest_at.isoformat()
        if verdict.supply_newest_at is not None
        else "none"
    )
    age = (
        f"{verdict.age_seconds / _SECONDS_PER_MINUTE:.1f}"
        if verdict.age_seconds is not None
        else "unknown"
    )
    text = (
        f":rotating_light: *STALE PROJECTION* — `{check.supply.relation}` newest row {newest} "
        f"({age} min old, limit {check.max_age_seconds / _SECONDS_PER_MINUTE:g} min) "
        f"while {verdict.demand_distinct_active} distinct `{check.demand.distinct_column}` "
        f"wrote `{check.demand.relation}` in the last {check.demand.window_seconds / _SECONDS_PER_MINUTE:g} min\n"
        f"correlation_id={correlation_id}"
    )
    return ModelFreshnessSlackCommand(
        channel=channel,
        text=text,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )
