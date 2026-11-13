# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: pure decision of whether to re-probe a revoked pool label."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_claude_pool_revoke_recheck import (
    ModelRevokeRecheckCommand,
    ModelRevokeRecheckRequest,
    ModelRevokeRecheckResult,
)


class HandlerRevokeRecheck:
    """Decides, per revoked label, whether to probe it and what to do with it."""

    def handle(self, request: ModelRevokeRecheckRequest) -> ModelRevokeRecheckResult:
        if request.policy is None:
            return ModelRevokeRecheckResult(
                label=request.label,
                action="not_configured",
                reason="revoke_recheck is not configured on this host: the mark is kept",
                command=None,
            )
        if request.revoked_at is None:
            return ModelRevokeRecheckResult(
                label=request.label,
                action="not_revoked",
                reason="the label carries no revoked mark: nothing to recheck",
                command=None,
            )
        if request.probe is None:
            interval = timedelta(seconds=request.policy.interval_seconds)
            due = request.last_probe_at is None or (
                request.now - request.last_probe_at >= interval
            )
            if not due:
                return ModelRevokeRecheckResult(
                    label=request.label,
                    action="not_due",
                    reason=(
                        "the label is revoked but the probe interval has not "
                        "elapsed since the last probe: wait"
                    ),
                    command=None,
                )
            return ModelRevokeRecheckResult(
                label=request.label,
                action="probe",
                reason="the label is revoked and the probe interval has elapsed: probe it",
                command=None,
            )

        probe = request.probe
        if probe.verdict == "ok":
            return ModelRevokeRecheckResult(
                label=request.label,
                action="clear",
                reason="the probe answered ok: the revoked mark is cleared",
                command=ModelRevokeRecheckCommand(
                    label=request.label,
                    fingerprint=request.fingerprint,
                    expected_revoked_at=request.revoked_at,
                    action="clear",
                    new_until=None,
                    probed_at=probe.probed_at,
                ),
            )
        if probe.verdict == "limited":
            # A limited answer means the token authenticates, so the revoke is
            # not real; a reset after now is carried as the mark to stamp next.
            new_until = (
                probe.reset_at
                if probe.reset_at is not None and probe.reset_at > request.now
                else None
            )
            return ModelRevokeRecheckResult(
                label=request.label,
                action="clear",
                reason=(
                    "the probe answered limited, so the token authenticates and "
                    "the revoke is not real: the revoked mark is cleared"
                ),
                command=ModelRevokeRecheckCommand(
                    label=request.label,
                    fingerprint=request.fingerprint,
                    expected_revoked_at=request.revoked_at,
                    action="clear",
                    new_until=new_until,
                    probed_at=probe.probed_at,
                ),
            )
        if probe.verdict == "refused":
            if request.cause == "challenged":
                return ModelRevokeRecheckResult(
                    label=request.label,
                    action="confirm",
                    reason=(
                        "the probe answered refused on a challenged label: the "
                        "revocation is confirmed"
                    ),
                    command=ModelRevokeRecheckCommand(
                        label=request.label,
                        fingerprint=request.fingerprint,
                        expected_revoked_at=request.revoked_at,
                        action="confirm",
                        new_until=None,
                        probed_at=probe.probed_at,
                    ),
                )
            return ModelRevokeRecheckResult(
                label=request.label,
                action="keep",
                reason=(
                    "the probe answered refused, so the token keeps failing: the "
                    "mark is kept and the probe time is recorded"
                ),
                command=ModelRevokeRecheckCommand(
                    label=request.label,
                    fingerprint=request.fingerprint,
                    expected_revoked_at=request.revoked_at,
                    action="keep",
                    new_until=None,
                    probed_at=probe.probed_at,
                ),
            )
        return ModelRevokeRecheckResult(
            label=request.label,
            action="keep",
            reason=(
                "the probe answered error, which proves nothing: no probe time "
                "is recorded and the label is probed again on the next tick"
            ),
            command=None,
        )
