# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``onex scm`` verbs served by the GitHub repo gateway node."""

from __future__ import annotations

from typing import Final

from omnimarket.models.scm import EnumScmNode, ModelScmVerb

_G = EnumScmNode.GATEWAY

SCM_GATEWAY_VERBS: Final[tuple[ModelScmVerb, ...]] = tuple(
    (
        ModelScmVerb(verb=v, node=_G, operation=op, replaces=gh)
        for v, op, gh in (
            ("pr-review", "review_gate", "gh pr view --json reviewDecision"),
            ("pr-ticket", "ticket_ref", "gh pr view --json headRefName"),
            (
                "branch-protection",
                "branch_protection",
                "gh api repos/<o>/<r>/branches/<b>/protection",
            ),
            ("job-log", "job_log_tail", "gh api repos/<o>/<r>/actions/jobs/<id>/logs"),
            (
                "commit-check-runs",
                "commit_check_runs",
                "gh api repos/<o>/<r>/commits/<sha>/check-runs",
            ),
            (
                "pr-merged",
                "merged_pr",
                "gh pr view --json state,mergedAt,mergeCommit,headRefOid",
            ),
            (
                "pr-threads",
                "pr_review_threads",
                "gh api graphql (pullRequest reviewThreads)",
            ),
            (
                "code-scanning-alerts",
                "code_scanning_alerts",
                "gh api repos/<o>/<r>/code-scanning/alerts",
            ),
        )
    )
)
