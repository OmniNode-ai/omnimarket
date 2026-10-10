# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fixer_dispatcher — routes PR stall events to the correct fixer node.

Input: ModelFixerDispatchRequest (stall event + PR context)
Output: ModelFixerDispatchResult (dispatch spec for the right fixer)

Fixer routing table:
    RED (CI failing)          -> node_pr_lifecycle_fix_effect (code_failure)
    CONFLICTED (merge conflict) -> node_pr_lifecycle_fix_effect (conflict)
    BEHIND (needs rebase)     -> node_pr_lifecycle_fix_effect (conflict, update-branch)
    DEPLOY_GATE               -> deploy-gate skip token (emit advisory)
    UNKNOWN / STALE            -> escalate (no auto-fix)

This is the structural fix for passive observation: detection without this
node is just reports.  OMN-9403.
"""
