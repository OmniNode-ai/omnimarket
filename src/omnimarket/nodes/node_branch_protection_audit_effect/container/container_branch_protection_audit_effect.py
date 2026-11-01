# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_audit import (
    HandlerBranchProtectionAudit,
)
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_policy_load import (
    HandlerBranchProtectionPolicyLoad,
)
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_github_api_read import (
    HandlerGitHubApiRead,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_fetch import (
        HandlerBranchProtectionFetch,
    )


def build_handler_branch_protection_policy_load() -> HandlerBranchProtectionPolicyLoad:
    """Build the policy loader."""
    return HandlerBranchProtectionPolicyLoad()


def build_handler_branch_protection_fetch() -> HandlerBranchProtectionFetch:
    """Build the snapshot fetcher without a circular import of its reader binding."""
    from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_fetch import (
        HandlerBranchProtectionFetch,
    )

    return HandlerBranchProtectionFetch()


def build_handler_branch_protection_audit() -> HandlerBranchProtectionAudit:
    """Build the branch protection auditor."""
    return HandlerBranchProtectionAudit()


def build_handler_github_api_read() -> HandlerGitHubApiRead:
    """Build the GitHub document reader."""
    return HandlerGitHubApiRead()
