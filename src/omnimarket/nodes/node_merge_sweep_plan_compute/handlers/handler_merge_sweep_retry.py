# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a merge sweep does with a lane whose runner exited without a host (OMN-20676).

A port of the sweep workflow's retry loop. Exit 75 means no lab host qualifies now: wait and
start the lane again, up to ``retries`` times, never on the launching host. Exit 77 means the
chosen host's Claude account hit a usage limit and exit 79 that its login expired: the host is
marked, and the lane starts once more on another host, whose result is final. Any other exit
code, or a retry already spent, accepts the receipt as it is.
"""

from __future__ import annotations

from ..models import (
    DEFAULT_RETRIES,
    DEFAULT_RETRY_WAIT_MIN,
    EXIT_HOST_AUTH_EXPIRED,
    EXIT_HOST_LIMITED,
    EXIT_NO_HOST,
    RETRIES_CEILING,
    RETRY_WAIT_CEILING_MIN,
    ModelMergeSweepRetryRequest,
    ModelMergeSweepRetryResult,
)


def retries_of(raw: int | None) -> int:
    """The retry budget: absent reads as 3, anything else is clamped to 0..6."""
    return DEFAULT_RETRIES if raw is None else max(0, min(RETRIES_CEILING, raw))


def retry_wait_min_of(raw: int | None) -> int:
    """Minutes to wait between starts: absent or zero reads as 5, anything else is clamped to 1..30."""
    return max(1, min(RETRY_WAIT_CEILING_MIN, raw or DEFAULT_RETRY_WAIT_MIN))


class HandlerMergeSweepRetry:
    """Decide the next move for one lane from its latest receipt's exit code."""

    def handle(
        self, request: ModelMergeSweepRetryRequest
    ) -> ModelMergeSweepRetryResult:
        retries = retries_of(request.retries)
        wait = retry_wait_min_of(request.retry_wait_min)
        if (
            request.exit_code == EXIT_NO_HOST
            and request.waits_used < retries
            and not request.host_retry_used
        ):
            action, wait_min = "wait_and_retry", wait
        elif (
            request.exit_code in (EXIT_HOST_LIMITED, EXIT_HOST_AUTH_EXPIRED)
            and not request.host_retry_used
        ):
            action, wait_min = "retry_other_host", None
        else:
            action, wait_min = "accept", None
        return ModelMergeSweepRetryResult(
            action=action, wait_min=wait_min, retries=retries, retry_wait_min=wait
        )
