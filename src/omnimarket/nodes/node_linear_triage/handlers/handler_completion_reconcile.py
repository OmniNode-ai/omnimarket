# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCompletionReconcile — dispatchable reverse-path DoD reconciler (OMN-14915).

A COMPUTE decision over a batch of pre-assembled completion facts. The pure
``handle`` path produces a fail-closed verdict report and never mutates Linear.
The separate, explicitly-guarded ``apply_reverts`` performs the actual revert
ONLY when ``apply_changes=True`` (dry-run is the default) and ONLY for
``REVERT_REQUIRED`` verdicts — ``KEEP`` and ``FLAG_FOR_REVIEW`` never drive a
mutation. Keeping the mutation out of ``handle`` means dispatching the node can
never revert the board on its own; the scheduled apply is a deliberate,
human-gated step (OMN-14915 remainder).
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    LinearClientProtocol,
)
from omnimarket.nodes.node_linear_triage.models.model_completion_reconcile import (
    EnumCompletionVerdict,
    ModelCompletionReconcileReport,
    ModelCompletionReconcileStartCommand,
    ModelCompletionVerdictResult,
)
from omnimarket.nodes.node_linear_triage.services.completion_reconcile import (
    reconcile_batch,
)

_log = logging.getLogger(__name__)

_REVERT_COMMENT_TEMPLATE = (
    "Reverted to {prior_state} by the completion reconciler (OMN-14915).\n\n"
    "This ticket was flipped to Done with **no durable evidence** (no merged "
    "implementing PR, no OCC receipt, no all-children-done roll-up) and bears "
    "the Linear `autoCloseChildIssues` cascade fingerprint (same-second "
    "sibling/parent batch). Per the CLAUDE.md deterministic-truth doctrine, a "
    "Done flip requires durable evidence that the ticket's own stated acceptance "
    "criteria were met — a server-side parent-completion cascade is not "
    "evidence.\n\n"
    "Reverted so the Definition of Done can be evidenced before this ticket is "
    "closed again. Reconciler verdict: {reason}"
)


class HandlerCompletionReconcile:
    """Reverse-path DoD reconciler handler.

    ``handle`` is pure (decision only). ``apply_reverts`` is the guarded EFFECT.
    """

    def handle(
        self,
        payload: ModelCompletionReconcileStartCommand | dict[str, object],
    ) -> ModelCompletionReconcileReport | dict[str, object]:
        """Decide verdicts for a batch. Pure — never mutates Linear.

        Supports both calling conventions used across omnimarket nodes:
        typed (``ModelCompletionReconcileStartCommand`` -> report model) and the
        RuntimeLocal dict shim (``dict`` -> ``dict``).
        """
        if isinstance(payload, dict):
            command = ModelCompletionReconcileStartCommand(**payload)
            return self._decide(command).model_dump(mode="json")
        return self._decide(payload)

    def _decide(
        self, command: ModelCompletionReconcileStartCommand
    ) -> ModelCompletionReconcileReport:
        results = reconcile_batch(command.completions)
        return _build_report(results, applied=False, reverted_ids=[])

    def apply_reverts(
        self,
        *,
        client: LinearClientProtocol,
        results: list[ModelCompletionVerdictResult],
        apply_changes: bool = False,
    ) -> ModelCompletionReconcileReport:
        """Revert every ``REVERT_REQUIRED`` verdict when ``apply_changes`` is True.

        Side-effect-guarded and fail-closed: ``apply_changes=False`` (the default)
        performs ZERO mutations and only reports what it WOULD revert. Only
        ``REVERT_REQUIRED`` drives a mutation; ``KEEP`` and ``FLAG_FOR_REVIEW``
        never do. Each revert writes the prior state and an explanatory comment
        citing the unmet-criteria class.
        """
        reverted_ids: list[str] = []
        for r in results:
            if r.verdict is not EnumCompletionVerdict.REVERT_REQUIRED:
                continue
            if not apply_changes:
                _log.info(
                    "[dry-run] would revert %s -> %s (%s)",
                    r.ticket_id,
                    r.prior_state_name,
                    r.reason,
                )
                continue
            if not r.linear_id:
                _log.warning(
                    "skipping revert for %s: facts carried no linear_id", r.ticket_id
                )
                continue
            client.save_issue(issue_id=r.linear_id, state=r.prior_state_name)
            client.save_comment(
                issue_id=r.linear_id,
                body=_REVERT_COMMENT_TEMPLATE.format(
                    prior_state=r.prior_state_name, reason=r.reason
                ),
            )
            reverted_ids.append(r.ticket_id)
        return _build_report(results, applied=apply_changes, reverted_ids=reverted_ids)


def _build_report(
    results: list[ModelCompletionVerdictResult],
    *,
    applied: bool,
    reverted_ids: list[str],
) -> ModelCompletionReconcileReport:
    keep = sum(1 for r in results if r.verdict is EnumCompletionVerdict.KEEP)
    flag = sum(1 for r in results if r.verdict is EnumCompletionVerdict.FLAG_FOR_REVIEW)
    revert = sum(
        1 for r in results if r.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    )
    return ModelCompletionReconcileReport(
        results=results,
        keep_count=keep,
        flag_count=flag,
        revert_count=revert,
        applied=applied,
        reverted_ticket_ids=reverted_ids,
    )


__all__ = ["HandlerCompletionReconcile"]
