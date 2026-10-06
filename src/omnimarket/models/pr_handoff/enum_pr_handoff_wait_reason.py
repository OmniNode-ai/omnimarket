# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a handoff waits: each is a fact the next PR watcher observation can change."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffWaitReason(StrEnum):
    """Why a handoff waits: each is a fact the next PR watcher observation can change."""

    PR_NOT_OBSERVED = "pr_not_observed"
    """The watcher has not observed the PR yet (a PR the lane just opened)."""

    HEAD_NOT_OBSERVED = "head_not_observed"
    """The newest observation predates the request and shows another head: the push is not observed yet."""

    OBSERVATION_STALE = "observation_stale"
    """The newest observation is older than the watcher's full-resync window before the request: the watcher may have stopped, so its view is not trusted (handoff_row.sh's freshness rule)."""

    DRAFT = "draft"
    """The PR is a draft and needs is not companion."""


__all__: list[str] = ["EnumPrHandoffWaitReason"]
