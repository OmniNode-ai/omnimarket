"""The legacy worktree audit node: classify every git worktree under a root (OMN-19740).

The audit lives in this node's handler; it is reached through the
``onex-legacy-worktree-audit`` console script.
"""

from __future__ import annotations

from .handlers.handler_legacy_worktree_audit import HandlerLegacyWorktreeAudit, main

__all__ = ["HandlerLegacyWorktreeAudit", "main"]
