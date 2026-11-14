# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a repeating install failure should raise, hold, or forget (OMN-21663).

THE RECORDED CASE. The landing controller's own update failed 207 times, each failure a row of its
install-failures file, and no alarm named it: nothing counted the rows or raised on their repeat.
This decision is pure: the caller reads the rows and the persisted state and passes them in, with
the threshold, window and reraise bound from its overlay.

WHAT IS DECIDED. The failures inside the window, counted against the threshold. Below it the alarm
is OK and forgets the last raise. At or above it, a fresh alarm raises once; a later one raises
again only after the reraise bound, and holds in between (OMN-21663).
"""

from __future__ import annotations

from datetime import timedelta

from ..models import (
    ModelInstallFailureAlarmRequest,
    ModelInstallFailureAlarmResult,
    ModelInstallFailureState,
)

__all__ = ["HandlerInstallFailureAlarm", "ModelInstallFailureAlarmResult"]


class HandlerInstallFailureAlarm:
    """Count the install failures in the window and decide raise, hold, or forget."""

    def handle(
        self, request: ModelInstallFailureAlarmRequest
    ) -> ModelInstallFailureAlarmResult:
        """Return the verdict, the count, the raise count, and the state to persist."""
        previous = request.previous
        if not request.readable:
            return ModelInstallFailureAlarmResult(
                verdict="UNREADABLE",
                count=0,
                raise_count=previous.raise_count,
                state=previous,
            )
        window = timedelta(minutes=request.window_minutes)
        cutoff = request.now - window
        # Rows with a future stamp count too: a clock skew is not a reason to hide a failure.
        counted = [row for row in request.failures if row.at >= cutoff]
        count = len(counted)
        if count < request.threshold:
            return ModelInstallFailureAlarmResult(
                verdict="OK",
                count=count,
                raise_count=0,
                state=ModelInstallFailureState(),
            )
        if previous.raised_at is None:
            raise_count = 1
        elif (
            request.reraise_after_minutes > 0
            and request.now - previous.raised_at
            >= timedelta(minutes=request.reraise_after_minutes)
        ):
            raise_count = previous.raise_count + 1
        else:
            return ModelInstallFailureAlarmResult(
                verdict="HELD",
                count=count,
                raise_count=previous.raise_count,
                state=previous,
            )
        first = min(counted, key=lambda row: row.at)
        newest = max(counted, key=lambda row: row.at)
        age = int((request.now - first.at).total_seconds() // 60)
        alert = (
            f"INSTALL FAILING {count} times in the last {request.window_minutes} minutes "
            f"since {first.at:%Y-%m-%dT%H:%M:%SZ} (first failure {age} minutes ago), "
            f"raised {raise_count} times; last: {newest.detail[:200]}"
        )
        state = ModelInstallFailureState(raised_at=request.now, raise_count=raise_count)
        return ModelInstallFailureAlarmResult(
            verdict="RAISE",
            count=count,
            raise_count=raise_count,
            alert_text=(alert,),
            state=state,
        )
