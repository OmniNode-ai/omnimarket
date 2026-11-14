# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reasons lab-fill work is skipped (OMN-20662).

Shared since OMN-20864: the idle-slot pr-land fallback rule, which lab-fill selection and the
merge-throughput tick both run, decides with the same reasons.
"""

from enum import StrEnum


class EnumLabFillSkipReason(StrEnum):
    """First matching selection gate."""

    OWNED = "owned"
    HANDED_OFF = "handed-off"
    UNCHANGED_INPUT = "unchanged-input"
    DISPATCH_LIMIT = "dispatch-limit"
    OUT_OF_SCOPE = "out-of-repository-scope"
    IMPLEMENTATION_MERGED = "implementation-merged"
    # OMN-22347: an earlier lane ended already-built or skipped-owned and a merged PR is known; the item
    # waits for its ticket to change and is listed for the closer.
    ALREADY_BUILT = "already-built"
    # OMN-20864: an in-force HOLD row names the PR or its repository.
    SKIP_HELD = "held"
    # OMN-20864: the PR's red, or its last landing lane's block, is a cause no lane can fix on it.
    SKIP_BLOCKED_UNFIXABLE = "blocked-unfixable"
    # OMN-23312: a delegation ticket whose assignee is not one of the deployment's delegation owners.
    OTHER_OWNER = "other-owner"
    # OMN-19998: a lane ended in a terminal outcome the deployment holds, inside the window, and the
    # ticket and its start criterion have not changed since; the fill waits instead of resending.
    TERMINAL_HELD = "terminal-held"
    # OMN-19998: the lane ended skipped-already-built with falsifiers not pass; the ticket is listed
    # for the readback path instead of a fill lane.
    READBACK = "readback"
