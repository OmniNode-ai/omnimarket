# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execution modes of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingGithubMode(StrEnum):
    """``dry_run`` records the requests and calls nothing; ``enforce`` sends them."""

    DRY_RUN = "dry_run"
    ENFORCE = "enforce"
