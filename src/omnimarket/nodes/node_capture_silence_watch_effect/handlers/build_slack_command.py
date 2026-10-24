# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a silence finding as one plain mrkdwn message."""

from uuid import UUID

from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    ModelCaptureSource,
    ModelCaptureWatchPolicy,
    ModelCaptureWatchSlackCommand,
    ModelCaptureWatchVerdict,
)

# Unit conversions, not alert thresholds.
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def build_slack_command(
    *,
    source: ModelCaptureSource,
    policy: ModelCaptureWatchPolicy,
    verdict: ModelCaptureWatchVerdict,
    channel: str,
    idempotency_key: str,
    correlation_id: UUID,
) -> ModelCaptureWatchSlackCommand:
    """All alarm facts and correlation context travel on the existing command."""
    newest = verdict.newest_at.isoformat() if verdict.newest_at is not None else "none"
    age = (
        f"{verdict.age_seconds / _SECONDS_PER_MINUTE:.1f}"
        if verdict.age_seconds is not None
        else "unknown"
    )
    if verdict.scope == "source":
        text = (
            f":rotating_light: *CAPTURE SOURCE STALE* — `{source.source_id}` "
            f"(`{source.relation}`) newest row {newest} "
            f"({age} min old, limit {verdict.bound_seconds / _SECONDS_PER_MINUTE:g} min)"
        )
    else:
        text = (
            f":warning: *CAPTURE SILENT* — `{verdict.actor}` wrote `{source.source_id}` "
            f"in the last {policy.active_window_seconds / _SECONDS_PER_HOUR:g} h; "
            f"newest row {newest} "
            f"({age} min old, limit {verdict.bound_seconds / _SECONDS_PER_MINUTE:g} min)"
        )
    return ModelCaptureWatchSlackCommand(
        channel=channel,
        text=f"{text}\ncorrelation_id={correlation_id}",
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )
