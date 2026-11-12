# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: pure decision of whether to re-probe a limited pool label."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_claude_pool_limit_recheck import (
    ModelLimitRecheckCommand,
    ModelLimitRecheckRequest,
    ModelLimitRecheckResult,
)


class HandlerLimitRecheck:
    """Decides, per limited label, whether to probe it and what to do with it."""

    def handle(self, request: ModelLimitRecheckRequest) -> ModelLimitRecheckResult:
        if request.policy is None:
            return ModelLimitRecheckResult(
                label=request.label,
                action="not_configured",
                reason="limit_recheck is not configured on this host: the mark is kept",
                command=None,
            )
        if request.until is None or request.until <= request.now:
            return ModelLimitRecheckResult(
                label=request.label,
                action="not_limited",
                reason="the label carries no limited-until mark after now: nothing to recheck",
                command=None,
            )
        if request.probe is None:
            interval = timedelta(seconds=request.policy.interval_seconds)
            due = request.last_probe_at is None or (
                request.now - request.last_probe_at >= interval
            )
            if not due:
                return ModelLimitRecheckResult(
                    label=request.label,
                    action="not_due",
                    reason=(
                        "the label is limited but the probe interval has not "
                        "elapsed since the last probe: wait"
                    ),
                    command=None,
                )
            return ModelLimitRecheckResult(
                label=request.label,
                action="probe",
                reason="the label is limited and the probe interval has elapsed: probe it",
                command=None,
            )

        probe = request.probe
        if probe.verdict == "ok":
            return ModelLimitRecheckResult(
                label=request.label,
                action="clear",
                reason="the probe answered ok: the limited-until mark is cleared",
                command=ModelLimitRecheckCommand(
                    label=request.label,
                    fingerprint=request.fingerprint,
                    expected_until=request.until,
                    action="clear",
                    new_until=None,
                    probed_at=probe.probed_at,
                ),
            )
        if probe.verdict == "limited":
            if (
                probe.reset_at is not None
                and probe.reset_at > request.now
                and probe.reset_at != request.until
            ):
                # A newer message replaces an older stamp: the new reset time
                # wins even when it is earlier than the old stamp.
                return ModelLimitRecheckResult(
                    label=request.label,
                    action="replace",
                    reason=(
                        "the probe answered limited with a reset at "
                        f"{probe.reset_at.isoformat()}, newer than the stamped "
                        f"{request.until.isoformat()}: the new reset replaces the mark"
                    ),
                    command=ModelLimitRecheckCommand(
                        label=request.label,
                        fingerprint=request.fingerprint,
                        expected_until=request.until,
                        action="replace",
                        new_until=probe.reset_at,
                        probed_at=probe.probed_at,
                    ),
                )
            return ModelLimitRecheckResult(
                label=request.label,
                action="keep",
                reason=(
                    "the probe answered limited with no newer reset: the mark is "
                    "kept and the probe time is recorded"
                ),
                command=ModelLimitRecheckCommand(
                    label=request.label,
                    fingerprint=request.fingerprint,
                    expected_until=request.until,
                    action="keep",
                    new_until=None,
                    probed_at=probe.probed_at,
                ),
            )
        return ModelLimitRecheckResult(
            label=request.label,
            action="keep",
            reason=(
                "the probe answered error, which proves nothing: no probe time "
                "is recorded and the label is probed again on the next tick"
            ),
            command=None,
        )
