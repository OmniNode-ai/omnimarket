# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrLifecycleBusObservation — the wire shape the runtime actually delivers.

OMN-17810. ``node_pr_lifecycle_state_reducer`` declares ``db_io.db_tables``
(``pr_lifecycle_ledger_entries``), so ``omnibase_infra``'s auto-wiring selects the
**projection dispatch arm** for it. That arm's contract is
``handle(input_data: dict)`` where ``input_data`` is the producer's own JSON
payload augmented with the runtime-injected ``_db`` / ``_event_type`` / ``_topic``
keys — NOT the in-process ``{"state": ..., "event": ...}`` RuntimeLocal envelope
the handler's shim was written for.

Nothing on the bus ever carried that RuntimeLocal envelope. Measured on the .201
dev lane 2026-09-07: of the reducer's eight subscribed topics, exactly one has
ever carried a record — ``pr-lifecycle-fix-completed.v1`` at HIGH-WATERMARK 5064
— and the seven others (the sweep-start command topic the ``{"state", "event"}``
shim was written for included) sit at HIGH-WATERMARK 0.

This model is the typed view of that one real producer shape. It is deliberately
NOT a re-declaration of the producer's model: importing
``node_pr_lifecycle_fix_effect``'s ``ModelPrLifecycleFixResult`` here would
cross-import a sibling node package, which this repo forbids (see the same note
on ``omnimarket.projection.pr_ledger_projection``). Instead the producer's field
names are declared as explicit validation aliases — a closed, named set, not a
structural guess.

Fail-fast, by construction:
  * ``source_topic`` is validated against the closed set of topics this reducer
    can project. A subscribed topic that is not in that set raises a
    ``ValidationError`` naming the topic and the accepted set, which the runtime
    classifies as a CONTENT failure (``_is_projection_content_failure``) and
    routes to the DLQ with the offset advancing — the same safe disposition
    those (zero-traffic) topics have today, but with a diagnosis that names the
    real defect instead of a misleading "ModelPrLifecycleState correlation_id
    missing".
  * every field is required; there are no defaults standing in for absent
    producer data.

Related:
    - OMN-17810: this defect (100% of fix-completed events to the quarantine sink).
    - OMN-16939: registered the dispatcher that made this reachable.
    - OMN-16767: the wiring-arm class this re-manifests on a second contract.
    - OMN-13321: the pr_lifecycle_ledger_entries projection being written.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.topics import PR_LIFECYCLE_FIX_COMPLETED_TOPIC_V1

# The one subscribed topic whose producer emits a PR-scoped lifecycle
# observation this reducer can project into pr_lifecycle_ledger_entries.
# Resolved from the canonical topic registry, never spelled here.
PR_LIFECYCLE_FIX_COMPLETED_TOPIC = PR_LIFECYCLE_FIX_COMPLETED_TOPIC_V1

# Closed set. Adding a topic here is a decision that its producer emits a
# PR-scoped observation carrying every required field below — never a way to
# silence a DLQ row.
PROJECTABLE_BUS_TOPICS: frozenset[str] = frozenset({PR_LIFECYCLE_FIX_COMPLETED_TOPIC})


class ModelPrLifecycleBusObservation(BaseModel):
    """One PR-scoped lifecycle observation delivered by the runtime bus.

    ``extra="ignore"`` because the producer's payload legitimately carries
    fields this projection does not use (``occ_companion_verified``,
    ``delegated``, ``delegation_cost_usd``, …). It never stands in for a
    MISSING field: every field declared here is required.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    source_topic: str = Field(
        ...,
        description="Topic the runtime dispatched this observation from (_topic).",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation of the lifecycle run this observation belongs to.",
    )
    repo: str = Field(
        ...,
        min_length=1,
        description="GitHub repo slug, e.g. 'OmniNode-ai/omnimarket'.",
    )
    pr_number: int = Field(..., ge=1, description="GitHub PR number observed.")
    observed_at: datetime = Field(
        ...,
        validation_alias=AliasChoices("observed_at", "completed_at"),
        description="When the producer completed the observed action.",
    )
    initial_state: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("initial_state", "block_reason"),
        description="The PR's state entering the observed action (the block reason).",
    )
    action_applied: bool = Field(
        ...,
        validation_alias=AliasChoices("action_applied", "fix_applied"),
        description="Whether the producer actually dispatched its action.",
    )
    evidence: str = Field(
        ...,
        validation_alias=AliasChoices("evidence", "fix_action"),
        description="Operator-readable rationale the producer recorded.",
    )
    error: str | None = Field(
        default=None,
        description="Producer error message, when the action failed.",
    )

    @field_validator("source_topic")
    @classmethod
    def _topic_is_projectable(cls, value: str) -> str:
        if value not in PROJECTABLE_BUS_TOPICS:
            raise ValueError(
                f"node_pr_lifecycle_state_reducer has no ledger projection for "
                f"topic {value!r}; projectable topics are "
                f"{sorted(PROJECTABLE_BUS_TOPICS)}. The reducer subscribes to it "
                f"but its producer emits no PR-scoped observation, so no "
                f"pr_lifecycle_ledger_entries row can be built from it (OMN-17810)."
            )
        return value


__all__ = [
    "PROJECTABLE_BUS_TOPICS",
    "PR_LIFECYCLE_FIX_COMPLETED_TOPIC",
    "ModelPrLifecycleBusObservation",
]
