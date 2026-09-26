# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operations of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingGithubOperation(StrEnum):
    """The six GitHub operations the PR landing workflow may request.

    Order matches the contract's ``operations`` list.
    """

    RERUN_RUNS = "rerun_runs"
    UPDATE_BRANCH = "update_branch"
    ARM_AUTO_MERGE = "arm_auto_merge"
    ENQUEUE = "enqueue"
    DISARM = "disarm"
    READ_HEAD_CHECKS = "read_head_checks"
