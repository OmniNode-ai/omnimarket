# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFixerDispatcher — routes PR stall events to the correct fixer node.

Pure compute: maps stall category to a fixer dispatch spec.
Zero network calls, zero side effects. All routing is table-driven.

Routing table (stall category -> fixer):
    RED            -> node_ci_fix_effect   (CI failing)
    CONFLICTED     -> node_conflict_hunk_effect (merge conflict)
    BEHIND         -> node_rebase_effect   (needs rebase)
    DEPLOY_GATE    -> advisory (deploy-gate skip token)
    UNKNOWN/STALE  -> escalate (no auto-fix)

OMN-9403.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from omnimarket.nodes.node_fixer_dispatcher.models.model_fixer_dispatch import (
    EnumFixerAction,
    EnumStallCategory,
    ModelFixerDispatchRequest,
    ModelFixerDispatchResult,
)

_log = logging.getLogger(__name__)

# --- Load command topics from contract.yaml ----------------------------------


def _load_command_topics() -> dict[str, str]:
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with contract_path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return dict(data.get("routing", {}).get("command_topics", {}))


_CONTRACT_COMMAND_TOPICS: dict[str, str] = _load_command_topics()

# --- Routing table -----------------------------------------------------------

# Maps stall category to (action, target_node, confidence).
# Topics are resolved at init time from contract.yaml via _CONTRACT_COMMAND_TOPICS.
_ROUTING_TABLE: dict[str, tuple[str, str, float]] = {
    EnumStallCategory.RED: (
        EnumFixerAction.DISPATCH_CI_FIX,
        "node_ci_fix_effect",
        0.95,
    ),
    EnumStallCategory.CONFLICTED: (
        EnumFixerAction.DISPATCH_CONFLICT_RESOLVE,
        "node_conflict_hunk_effect",
        0.90,
    ),
    EnumStallCategory.BEHIND: (
        EnumFixerAction.DISPATCH_REBASE,
        "node_rebase_effect",
        0.90,
    ),
    EnumStallCategory.DEPLOY_GATE: (
        EnumFixerAction.DISPATCH_DEPLOY_GATE_SKIP,
        "",
        0.80,
    ),
}

# Categories that are safe to auto-dispatch (no human review needed).
_AUTO_DISPATCH_CATEGORIES: frozenset[str] = frozenset(
    {
        EnumStallCategory.RED,
        EnumStallCategory.CONFLICTED,
        EnumStallCategory.BEHIND,
    }
)


@runtime_checkable
class FixerPolicyProtocol(Protocol):
    """Optional policy hook for overriding routing decisions.

    Use for org-specific rules like "don't auto-fix release branches"
    or "require human approval for repos in deploy freeze."
    """

    def should_dispatch(
        self, request: ModelFixerDispatchRequest, action: str
    ) -> str | None:
        """Return None to allow dispatch, or a reason string to block it."""
        ...


class HandlerFixerDispatcher:
    """Routes PR stall events to the correct fixer node.

    Pure compute: all routing is table-driven. Zero network calls.
    """

    def __init__(self, policy: FixerPolicyProtocol | None = None) -> None:
        self._policy = policy

    def handle(self, request: ModelFixerDispatchRequest) -> ModelFixerDispatchResult:
        """Route the stall event to the correct fixer.

        Args:
            request: PR stall event with category and context.

        Returns:
            Dispatch spec for the target fixer node.
        """
        category = request.stall_category.lower().strip()
        _log.info(
            "Dispatching PR %s#%d (category=%s, stall_count=%d)",
            request.repo,
            request.pr_number,
            category,
            request.stall_count,
        )

        # Look up routing table
        route = _ROUTING_TABLE.get(category)
        if route is None:
            return self._escalate(
                request,
                f"No fixer registered for category '{category}' "
                f"(stall_count={request.stall_count})",
            )

        action, target_node, confidence = route
        target_topic = _CONTRACT_COMMAND_TOPICS.get(category, "")

        # Fail closed: if a non-advisory category has no topic in contract, escalate
        if target_node and not target_topic:
            return self._escalate(
                request,
                f"No command topic declared in contract for category '{category}'; "
                "add it to contract.yaml routing.command_topics to enable dispatch.",
            )

        # Policy gate: allow external rules to block dispatch
        if self._policy is not None:
            block_reason = self._policy.should_dispatch(request, action)
            if block_reason is not None:
                return self._escalate(
                    request,
                    f"Policy blocked dispatch: {block_reason}",
                )

        # Build payload hint for the target fixer
        payload_hint = self._build_payload_hint(request, category)

        # Deploy-gate is advisory (no target node to invoke)
        if category == EnumStallCategory.DEPLOY_GATE:
            return ModelFixerDispatchResult(
                pr_number=request.pr_number,
                repo=request.repo,
                action=EnumFixerAction.DISPATCH_DEPLOY_GATE_SKIP,
                target_node="",
                target_topic="",
                payload_hint=payload_hint,
                reason=(
                    f"Deploy-gate blocking: {request.blocking_reason}. "
                    "Advisory: add [skip-deploy-gate: ...] token to PR body."
                ),
                confidence=confidence,
            )

        reason = (
            f"Stall category '{category}' routed to {target_node} "
            f"(stall_count={request.stall_count}, blocking: {request.blocking_reason})"
        )

        _log.info(
            "Routed PR %s#%d -> %s (action=%s, confidence=%.2f)",
            request.repo,
            request.pr_number,
            target_node,
            action,
            confidence,
        )

        return ModelFixerDispatchResult(
            pr_number=request.pr_number,
            repo=request.repo,
            action=action,
            target_node=target_node,
            target_topic=target_topic,
            payload_hint=payload_hint,
            reason=reason,
            confidence=confidence,
        )

    def _escalate(
        self, request: ModelFixerDispatchRequest, reason: str
    ) -> ModelFixerDispatchResult:
        """Return an escalate result (no auto-fix available)."""
        _log.warning(
            "Escalating PR %s#%d: %s",
            request.repo,
            request.pr_number,
            reason,
        )
        return ModelFixerDispatchResult(
            pr_number=request.pr_number,
            repo=request.repo,
            action=EnumFixerAction.ESCALATE,
            target_node="",
            target_topic="",
            payload_hint={},
            reason=reason,
            confidence=0.0,
        )

    @staticmethod
    def _build_payload_hint(
        request: ModelFixerDispatchRequest, category: str
    ) -> dict[str, str]:
        """Pre-fill payload fields for the target fixer command."""
        hint: dict[str, str] = {
            "pr_number": str(request.pr_number),
            "repo": request.repo,
        }
        if request.head_sha:
            hint["head_sha"] = request.head_sha
        if request.branch_name:
            hint["branch_name"] = request.branch_name
        if request.dry_run:
            hint["dry_run"] = "true"

        # Category-specific hints
        if category == EnumStallCategory.RED:
            hint["fix_type"] = "ci"
        elif category == EnumStallCategory.CONFLICTED:
            hint["fix_type"] = "conflict"
        elif category == EnumStallCategory.BEHIND:
            hint["fix_type"] = "rebase"

        return hint


__all__: list[str] = ["FixerPolicyProtocol", "HandlerFixerDispatcher"]
