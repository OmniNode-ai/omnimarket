# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State, command and result models for the review equivalence reducer (OMN-21190).

An equivalence edge says occurrence key A is the same defect as occurrence key B. It
is recorded with the evidence for the match, is auditable (every transition is kept)
and is revocable. The graph revision counts decisions; label cutoffs and scoring
manifests pin it.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumEquivalenceAction(StrEnum):
    ADMIT = "ADMIT"
    REJECT = "REJECT"
    REVOKE = "REVOKE"


class EnumEquivalenceEdgeStatus(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


class EnumEquivalenceOutcome(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    DUPLICATE = "DUPLICATE"
    REFUSED = "REFUSED"


class ModelMatchEvidence(BaseModel):
    """Why two occurrences are called the same defect: the matcher's score and the joined original findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    similarity: float = Field(ge=0, le=1)
    matcher: str = Field(
        min_length=1,
        description="The candidate source, e.g. node_finding_aggregator_compute.",
    )
    original_finding_refs: tuple[str, ...] = ()


class ModelEdgeTransition(BaseModel):
    """One decision on an edge, at the graph revision it created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=1)
    status: EnumEquivalenceEdgeStatus
    reason: str
    decided_by: str
    evidence: ModelMatchEvidence | None = None


class ModelEquivalenceEdge(BaseModel):
    """An unordered pair of occurrence keys (a < b) and its decision history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    occurrence_key_a: str
    occurrence_key_b: str
    status: EnumEquivalenceEdgeStatus
    history: tuple[ModelEdgeTransition, ...]

    @property
    def evidence(self) -> ModelMatchEvidence | None:
        """The evidence the current status was decided on, else the latest evidence recorded."""
        for transition in reversed(self.history):
            if transition.evidence is not None:
                return transition.evidence
        return None


class ModelEquivalenceGraphState(BaseModel):
    """The graph: a revision counter and its edges, sorted by edge id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(default=0, ge=0)
    edges: tuple[ModelEquivalenceEdge, ...] = ()


class ModelEquivalenceCommand(BaseModel):
    """One decision on one candidate pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumEquivalenceAction
    occurrence_key_a: str = Field(min_length=1)
    occurrence_key_b: str = Field(min_length=1)
    evidence: ModelMatchEvidence | None = None
    reason: str = ""
    decided_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> ModelEquivalenceCommand:
        if self.occurrence_key_a == self.occurrence_key_b:
            raise ValueError("an equivalence edge joins two different occurrence keys")
        if self.action is EnumEquivalenceAction.ADMIT and self.evidence is None:
            raise ValueError("admitting an edge requires the evidence for the match")
        return self


class ModelEquivalenceReducerInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    state: ModelEquivalenceGraphState
    command: ModelEquivalenceCommand


class ModelDefectKeyAssignment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence_key: str
    defect_key: str


class ModelEquivalenceReducerOutput(BaseModel):
    """The new state and what the decision did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    state: ModelEquivalenceGraphState
    outcome: EnumEquivalenceOutcome
    revision_before: int = Field(ge=0)
    revision_after: int = Field(ge=0)
    edge_id: str
    refusal_reason: str | None = None
    affected_occurrence_keys: tuple[str, ...] = Field(
        default=(),
        description="Keys whose defect key may have changed: the component(s) the decision touched.",
    )
    relabel_occurrence_keys: tuple[str, ...] = Field(
        default=(),
        description="On a revocation, the keys whose transferred labels are invalidated and must be relabelled.",
    )
    defect_keys: tuple[ModelDefectKeyAssignment, ...] = Field(
        default=(), description="The affected keys' defect keys in the new state."
    )
