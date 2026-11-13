# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSiblingPremise: one release-train sibling premise decision, as a pure function.

Definition-B compute: ``handle(request: ModelSiblingPremiseRequest) -> ModelSiblingPremiseVerdict``.
No clock, no I/O, no model, no envelope. The same request gives byte-identical
verdicts in any input order.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.enum_sibling_premise import (
    EnumSiblingPremiseState,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.errors import (
    SiblingPremiseInputError,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_request import (
    ModelSiblingPremiseRequest,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_verdict import (
    ModelSiblingPremiseVerdict,
)

_OUTCOME_TO_STATE: dict[EnumDevMergeOutcome, EnumSiblingPremiseState] = {
    EnumDevMergeOutcome.LANE_BEHIND: EnumSiblingPremiseState.BEHIND,
    EnumDevMergeOutcome.DEPLOY_NOT_DONE: EnumSiblingPremiseState.DEPLOY_NOT_DONE,
    EnumDevMergeOutcome.DEADLINE_MISSED: EnumSiblingPremiseState.DEADLINE_MISSED,
    EnumDevMergeOutcome.LANE_UNREADABLE: EnumSiblingPremiseState.LANE_UNREADABLE,
}


def covering_shas(request: ModelSiblingPremiseRequest) -> frozenset[str]:
    """The set of merge SHAs that cover the candidate: the candidate plus its descendants."""
    return frozenset([request.candidate_sha, *request.descendant_shas])


def _sort_key(r: ModelDevMergeRedeployReceipt) -> tuple[datetime, str, str]:
    """Sort key for tie-breaking: earliest observed_at, then smaller merge_sha, then smaller str(correlation_id)."""
    return (r.observed_at, r.merge_sha, str(r.correlation_id))


class HandlerSiblingPremise:
    """Pure definition-B handler: one sibling premise decision per request."""

    def handle(self, request: ModelSiblingPremiseRequest) -> ModelSiblingPremiseVerdict:
        """Decide whether the dev lane runs the candidate revision."""
        if request.candidate_merged_at > request.as_of:
            raise SiblingPremiseInputError("candidate merged after as_of")

        covers = covering_shas(request)
        relevant = tuple(
            r
            for r in request.receipts
            if r.repo == request.repo and r.merge_sha in covers
        )
        receipts_considered = len(relevant)

        # Rule 2: any VERIFIED receipt -> READY
        verified = [r for r in relevant if r.outcome is EnumDevMergeOutcome.VERIFIED]
        if verified:
            # Pick the receipt whose merge_sha equals candidate_sha if one exists,
            # else the one with the earliest observed_at (ties: smaller merge_sha, then smaller str(correlation_id)).
            candidate_receipts = [
                r for r in verified if r.merge_sha == request.candidate_sha
            ]
            if candidate_receipts:
                chosen = min(candidate_receipts, key=_sort_key)
            else:
                chosen = min(verified, key=_sort_key)
            detail = (
                f"merge {chosen.merge_sha[:12]} covers candidate {request.candidate_sha[:12]}; "
                f"lane revision {chosen.lane_revision[:12] if chosen.lane_revision else 'unknown'}"
            )
            return ModelSiblingPremiseVerdict(
                state=EnumSiblingPremiseState.READY,
                repo=request.repo,
                candidate_sha=request.candidate_sha,
                covering_merge_sha=chosen.merge_sha,
                lane_revision=chosen.lane_revision,
                correlation_id=chosen.correlation_id,
                receipts_considered=receipts_considered,
                detail=detail,
                as_of=request.as_of,
            )

        # Rule 3: no relevant receipts
        if not relevant:
            elapsed = (request.as_of - request.candidate_merged_at) / timedelta(
                seconds=1
            )
            if elapsed < request.pending_window_seconds:
                return ModelSiblingPremiseVerdict(
                    state=EnumSiblingPremiseState.PENDING,
                    repo=request.repo,
                    candidate_sha=request.candidate_sha,
                    receipts_considered=0,
                    detail="no receipt yet and the window is still open",
                    as_of=request.as_of,
                )
            return ModelSiblingPremiseVerdict(
                state=EnumSiblingPremiseState.ABSENT,
                repo=request.repo,
                candidate_sha=request.candidate_sha,
                receipts_considered=0,
                detail="no receipt covers the candidate after the window",
                as_of=request.as_of,
            )

        # Rule 4: take the relevant receipt with the latest observed_at (ties as in rule 2)
        latest = max(
            relevant, key=lambda r: (r.observed_at, r.merge_sha, str(r.correlation_id))
        )
        state = _OUTCOME_TO_STATE.get(
            latest.outcome, EnumSiblingPremiseState.NOT_EXERCISED
        )
        detail = f"outcome {latest.outcome.value}; {latest.message}"[:2000]
        return ModelSiblingPremiseVerdict(
            state=state,
            repo=request.repo,
            candidate_sha=request.candidate_sha,
            covering_merge_sha=latest.merge_sha,
            lane_revision=latest.lane_revision,
            correlation_id=latest.correlation_id,
            receipts_considered=receipts_considered,
            detail=detail,
            as_of=request.as_of,
        )


__all__ = [
    "HandlerSiblingPremise",
    "covering_shas",
]
