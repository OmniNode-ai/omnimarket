# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bridge from ProtocolDelegationFixAdapter to node_pr_delegated_fix_effect.

Keeps ``handler_pr_lifecycle_fix.py`` free of node_pr_delegated_fix_effect
import details — it only depends on the ``dispatch_delegated_fix`` Protocol.
Raises on any non-accepted outcome so the caller's two-strike/escalation
logic in ``HandlerPrLifecycleFix._route_delegatable_fix`` treats it exactly
like any other adapter failure (never a silent partial success).
"""

from __future__ import annotations

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixCommand,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    HandlerDelegatedFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)


class DelegatedFixAdapter:
    """Live ``ProtocolDelegationFixAdapter`` implementation."""

    def __init__(self, handler: HandlerDelegatedFix | None = None) -> None:
        self._handler = handler or HandlerDelegatedFix()

    async def dispatch_delegated_fix(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        command: ModelPrLifecycleFixCommand,
    ) -> str:
        delegated_command = ModelDelegatedFixCommand(
            correlation_id=command.correlation_id,
            repo=repo,
            pr_number=pr_number,
            ticket_id=ticket_id,
            block_reason=command.block_reason.value,
            changed_files=command.changed_files,
            diff_total_lines=command.diff_total_lines,
            dry_run=command.dry_run,
            requested_at=command.requested_at,
        )
        result = await self._handler.handle(delegated_command)
        if result.outcome != EnumDelegatedFixOutcome.ACCEPTED:
            raise RuntimeError(
                f"delegated fix not accepted: outcome={result.outcome} "
                f"detail={result.detail}"
            )
        return (
            f"delegated fix accepted on {repo}#{pr_number}: {result.detail} "
            f"(commit={result.commit_sha})"
        )


__all__: list[str] = ["DelegatedFixAdapter"]
