# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCapsuleEffectivenessFeedback -- the M5 closed-loop feedback edge.

Consume-side REDUCER/EFFECT (OMN-12845 / M5). Takes a scored runtime ROI row and
writes its effectiveness back onto the durable M2 capsule store keyed by the
deterministic ``capsule_hash``, so M3 context selection then re-ranks on the
live-updated score.

Attribution honesty (BAC plan theme-5, lines 119-121) is the load-bearing
invariant of this node:

  * a CONTROLLED_INTERVENTION row (randomized arm order, fixed model/temp) may
    carry an effectiveness CLAIM -- its effectiveness is folded onto the capsule
    via the established M2 projection write surface
    (``HandlerCapsuleStoreProjection.project``); the M2 store is the only writer
    of capsule_store, so this edge owns no bespoke write path; and
  * an OBSERVATIONAL row may ONLY generate a HYPOTHESIS -- it is rejected from
    the claim path and never folded onto a capsule as a measured score.

A scored row whose ``proof_class`` is not RUNTIME_OBSERVED_ONLY is also rejected
from the claim path: a capsule effectiveness claim is only ever written from a
live runtime measurement.

Topic strings live ONLY in ``contract.yaml`` -- the handler resolves them from
:mod:`omnimarket.events.topics` constants and cross-checks the contract at
construction time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.capsule_feedback import ModelScoredRuntimeRow
from omnimarket.events.topics import (
    CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1,
    CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1,
    CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1,
)
from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.models.model_feedback_result import (
    ModelCapsuleFeedbackResult,
)
from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
    ModelCapsuleScoredEvent,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

# Schema version is passed to the M2 capsule-store scored-event as a string and
# coerced by ModelCapsuleScoredEvent's EnumCapsuleSchemaVersion field; the M2
# store owns the schema-version vocabulary, so this edge does not reach into the
# M2 models package (cross-node reach-in rule, omnimarket CLAUDE.md).
_CAPSULE_SCHEMA_VERSION = "v1"

logger = logging.getLogger(__name__)

HANDLER_ID_CAPSULE_EFFECTIVENESS_FEEDBACK = "capsule-effectiveness-feedback"


def _load_contract(path: Path) -> dict[str, Any]:
    """Load the node's contract.yaml for construction-time topic resolution.

    Mirrors the runner's ``_load_yaml`` precedent: the topic strings live ONLY in
    contract.yaml and are resolved here rather than hardcoded in handler logic.
    """
    with open(path) as handle:  # node-purity-ok: OMN-12845 contract topic resolution
        data: dict[str, Any] = yaml.safe_load(handle)
    return data


class AttributionHonestyError(ValueError):
    """Raised when an observational (or non-runtime) row reaches the claim path.

    Only a CONTROLLED_INTERVENTION runtime-observed row may write an
    effectiveness claim onto a capsule; everything else is a hypothesis at most.
    """


class HandlerCapsuleEffectivenessFeedback:
    """Fold a scored runtime row onto the M2 capsule store with attribution honesty."""

    def __init__(self, contract_path: Path | None = None) -> None:
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        # Construction-time topic resolution from the node's own contract.yaml --
        # the canonical pattern (mirrors HandlerCapsuleStoreProjection.__init__)
        # for sourcing topics from the contract instead of hardcoding them; not a
        # runtime I/O dependency.
        contract = _load_contract(path)

        event_bus = contract.get("event_bus", {})
        subscribe_topics = list(event_bus.get("subscribe_topics", []))
        publish_topics = list(event_bus.get("publish_topics", []))

        # Cross-check the contract against the canonical topic constants so a
        # drift between contract.yaml and the topic registry fails fast.
        if CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1 not in subscribe_topics:
            raise ValueError(
                "contract.yaml must subscribe to "
                f"{CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1}"
            )
        if CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1 not in publish_topics:
            raise ValueError(
                f"contract.yaml must publish {CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1}"
            )
        if CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1 not in publish_topics:
            raise ValueError(
                f"contract.yaml must publish {CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1}"
            )

        self._subscribe_topic = CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1
        self._claim_topic = CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1
        self._hypothesis_topic = CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1

        # Reuse the M2 projection as the SOLE capsule_store writer -- this edge
        # owns no bespoke write path.
        self._capsule_store = HandlerCapsuleStoreProjection()

    @property
    def subscribe_topic(self) -> str:
        return self._subscribe_topic

    @property
    def claim_topic(self) -> str:
        return self._claim_topic

    @property
    def hypothesis_topic(self) -> str:
        return self._hypothesis_topic

    @staticmethod
    def _to_scored_event(row: ModelScoredRuntimeRow) -> ModelCapsuleScoredEvent:
        """Map a row to the M2 capsule-store scored-event shape.

        The schema version is supplied as a string the M2 model coerces into its
        EnumCapsuleSchemaVersion, so this edge never reaches into the M2 models
        package. The resulting event's ``identity()`` yields the deterministic
        capsule_hash used as the capsule key.
        """
        return ModelCapsuleScoredEvent(
            factor=row.factor,
            content=row.content,
            source_artifact=row.source_artifact,
            source_commit=row.source_commit,
            schema_version=_CAPSULE_SCHEMA_VERSION,
            validity_scope=row.validity_scope,
            final_success_rate=row.final_success_rate,
            first_pass_rate=row.first_pass_rate,
            cost_per_success_usd=row.cost_per_success_usd,
            event_timestamp=row.event_timestamp,
        )

    def write_effectiveness_claim(
        self,
        row: ModelScoredRuntimeRow,
        db: DatabaseAdapter,
    ) -> ModelCapsuleFeedbackResult:
        """Write a CONTROLLED_INTERVENTION row's effectiveness onto the capsule.

        Strict claim path: an observational or non-runtime-observed row is
        rejected with AttributionHonestyError rather than silently downgraded --
        a caller that explicitly requests the claim path must present a valid
        controlled measurement.
        """
        if row.proof_class is not EnumProofClass.RUNTIME_OBSERVED_ONLY:
            raise AttributionHonestyError(
                "a capsule effectiveness claim may only be written from a "
                f"runtime-observed row; got proof_class={row.proof_class.value!r}"
            )
        if not row.is_controlled:
            raise AttributionHonestyError(
                "an observational row cannot claim effectiveness onto a capsule; "
                "only a controlled-intervention row may carry an effectiveness "
                "claim (BAC attribution-honesty rule)"
            )

        scored_event = self._to_scored_event(row)
        capsule_hash = scored_event.identity().capsule_hash
        projection = self._capsule_store.project(scored_event, db)
        logger.info(
            "Wrote capsule effectiveness claim",
            extra={
                "handler": HANDLER_ID_CAPSULE_EFFECTIVENESS_FEEDBACK,
                "capsule_hash": capsule_hash,
                "routing_source": row.routing_source,
                "claim_topic": self._claim_topic,
            },
        )
        return ModelCapsuleFeedbackResult(
            effectiveness_claim_written=True,
            hypothesis_recorded=False,
            capsule_hash=capsule_hash,
            rows_upserted=projection.rows_upserted,
        )

    def _record_hypothesis(
        self, row: ModelScoredRuntimeRow
    ) -> ModelCapsuleFeedbackResult:
        """Route an observational row to the hypothesis path -- never a claim."""
        capsule_hash = self._to_scored_event(row).identity().capsule_hash
        logger.info(
            "Recorded capsule effectiveness hypothesis (not a measured claim)",
            extra={
                "handler": HANDLER_ID_CAPSULE_EFFECTIVENESS_FEEDBACK,
                "capsule_hash": capsule_hash,
                "routing_source": row.routing_source,
                "hypothesis_topic": self._hypothesis_topic,
            },
        )
        return ModelCapsuleFeedbackResult(
            effectiveness_claim_written=False,
            hypothesis_recorded=True,
            capsule_hash=capsule_hash,
            rows_upserted=0,
        )

    def project(
        self,
        row: ModelScoredRuntimeRow,
        db: DatabaseAdapter,
    ) -> ModelCapsuleFeedbackResult:
        """Route a scored runtime row by its attribution class.

        Controlled-intervention runtime rows write an effectiveness claim onto
        the capsule store; every other row is recorded as a hypothesis only.
        """
        if (
            row.is_controlled
            and row.proof_class is EnumProofClass.RUNTIME_OBSERVED_ONLY
        ):
            return self.write_effectiveness_claim(row, db)
        return self._record_hypothesis(row)

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Validates ``input_data`` (minus the injected ``_db`` adapter) into a
        ModelScoredRuntimeRow and delegates to :meth:`project`.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_data = {
            key: value for key, value in payload.items() if not key.startswith("_")
        }
        row = ModelScoredRuntimeRow.model_validate(event_data)
        return self.project(row, db_raw).model_dump(mode="json")


__all__ = [
    "HANDLER_ID_CAPSULE_EFFECTIVENESS_FEEDBACK",
    "AttributionHonestyError",
    "HandlerCapsuleEffectivenessFeedback",
]
