# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operations of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingGithubOperation(StrEnum):
    """The seven GitHub operations the PR landing workflow may request.

    Order matches the contract's ``operations`` list. ``read_pr_state`` was
    added by contract 1.1.0 (OMN-19831, plan revision 1 section 6).
    """

    RERUN_RUNS = "rerun_runs"
    UPDATE_BRANCH = "update_branch"
    ARM_AUTO_MERGE = "arm_auto_merge"
    ENQUEUE = "enqueue"
    DISARM = "disarm"
    READ_HEAD_CHECKS = "read_head_checks"
    READ_PR_STATE = "read_pr_state"
