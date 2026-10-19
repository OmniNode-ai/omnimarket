# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReviewRecordCompute: one review invocation in, one record per arm out.

Pure: no clock, no random ids. ``review_id`` is derived from the invocation id
and the arm's route, and each envelope's ``finding_id`` from the review id and the
finding's own id, so a redelivered invocation yields the same records. Every
record goes to the topic the contract declares under ``event_bus.publish_topics``;
the handler reads it from there and holds no copy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid5

from omnimarket.models.review_record.model_recorded_finding import (
    ModelOriginalFindingRef,
    ModelRecordedFinding,
)
from omnimarket.models.review_record.model_review_record import (
    ModelReviewArm,
    ModelReviewRecord,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_review_record_compute.models.model_review_invocation import (
    ModelReviewArmResult,
    ModelReviewInvocation,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"

REVIEW_ID_NAMESPACE = UUID("e80b054a-ba21-4bab-94d6-4d48a75a3bec")
FINDING_EMISSION_NAMESPACE = UUID("e581ddf8-92e7-4ae1-a5ef-2eabca6a8f38")


def review_id_for(invocation_id: UUID, route: str) -> UUID:
    """Stable id of one arm's record: an invocation names a route once."""
    return uuid5(REVIEW_ID_NAMESPACE, f"{invocation_id}|{route}")


def finding_emission_id_for(review_id: UUID, finding_id: UUID) -> UUID:
    """Stable id of one finding's emission in one record."""
    return uuid5(FINDING_EMISSION_NAMESPACE, f"{review_id}|{finding_id}")


@lru_cache(maxsize=4)
def review_recorded_topic(contract_path: Path = _CONTRACT) -> str:
    """The one topic the node's contract publishes records to."""
    topics = contract_publish_topics(contract_path)
    if len(topics) != 1:
        raise ValueError(
            f"{contract_path} declares {len(topics)} publish topics; "
            "the review record has exactly one"
        )
    return topics[0]


def _record_for_arm(
    invocation: ModelReviewInvocation, arm: ModelReviewArmResult
) -> ModelReviewRecord:
    review_id = review_id_for(invocation.invocation_id, arm.route)
    return ModelReviewRecord(
        review_id=review_id,
        invocation_id=invocation.invocation_id,
        arm=ModelReviewArm(
            route=arm.route,
            requested_settings=arm.requested_settings,
            served=arm.served,
        ),
        rubric_hash=invocation.rubric_hash,
        prompt_template_id=invocation.prompt_template_id,
        target=invocation.target,
        started_at=arm.started_at,
        finished_at=arm.finished_at,
        verdict=arm.verdict,
        findings=tuple(
            ModelRecordedFinding.from_finding(
                finding,
                finding_id=finding_emission_id_for(review_id, finding.id),
                original_finding_ref=ModelOriginalFindingRef(
                    finding_id=finding.id, review_id=review_id
                ),
            )
            for finding in arm.findings
        ),
        cost=arm.cost,
        source_node=invocation.source_node,
        execution_status=arm.execution_status,
        status_reason=arm.status_reason,
        principal=invocation.principal,
    )


class HandlerReviewRecordCompute:
    """COMPUTE handler: an invocation's arms become one record each."""

    @property
    def topic(self) -> str:
        return review_recorded_topic()

    def handle(self, request: ModelReviewInvocation) -> list[ModelReviewRecord]:
        return [_record_for_arm(request, arm) for arm in request.arms]


__all__: list[str] = [
    "HandlerReviewRecordCompute",
    "finding_emission_id_for",
    "review_id_for",
    "review_recorded_topic",
]
