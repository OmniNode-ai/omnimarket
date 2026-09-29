"""Typed board probe event and durable row models (OMN-19937)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from omnimarket.nodes.node_projection_board_probe_results.models.enum_board_probe_outcome import (
    EnumBoardProbeOutcome,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
BoardProbeResultKey = tuple[str, str, str, str, str, str]
VerdictRequest = tuple[str, str, str, str, str, str]


class ModelBoardProbeResultPayload(BaseModel):
    """The board-probe-result.v1 domain payload, with no transport envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: NonEmptyString
    subject_kind: NonEmptyString
    subject: NonEmptyString
    repo: NonEmptyString
    sha: NonEmptyString
    surface_instance: NonEmptyString
    execution_id: NonEmptyString
    outcome: EnumBoardProbeOutcome
    reasons: list[NonEmptyString]
    evidence_items: list[NonEmptyString]
    finished_at: datetime

    @field_validator("finished_at", mode="before")
    @classmethod
    def _finished_at_is_iso_text(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("finished_at must be an ISO-8601 UTC string")
        return value

    @field_validator("finished_at")
    @classmethod
    def _finished_at_is_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ValueError("finished_at must be an ISO-8601 UTC timestamp")
        if offset.total_seconds() != 0:
            raise ValueError("finished_at must be UTC")
        return value.astimezone(UTC)

    @property
    def partition_key(self) -> str:
        """The producer partition key declared by the event contract."""
        return self.subject


class ModelBoardProbeResultEvent(ModelBoardProbeResultPayload):
    """The payload plus broker ordering metadata consumed by the pure fold."""

    source_offset: int = Field(ge=0, exclude=True)

    @property
    def key(self) -> BoardProbeResultKey:
        return (
            self.check_id,
            self.subject_kind,
            self.repo,
            self.sha,
            self.surface_instance,
            self.execution_id,
        )


class ModelBoardProbeResultRow(BaseModel):
    """One materialized probe execution at the table's exact grain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: NonEmptyString
    subject_kind: NonEmptyString
    subject: NonEmptyString
    repo: NonEmptyString
    sha: NonEmptyString
    surface_instance: NonEmptyString
    execution_id: NonEmptyString
    outcome: EnumBoardProbeOutcome
    reasons: tuple[NonEmptyString, ...]
    evidence_items: tuple[NonEmptyString, ...]
    finished_at: datetime
    source_offset: int = Field(ge=0)

    @property
    def key(self) -> BoardProbeResultKey:
        return (
            self.check_id,
            self.subject_kind,
            self.repo,
            self.sha,
            self.surface_instance,
            self.execution_id,
        )


class ModelBoardProbeResultsProjectionResult(BaseModel):
    """Definition-B result returned by the pure handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelBoardProbeResultRow


__all__ = [
    "BoardProbeResultKey",
    "ModelBoardProbeResultEvent",
    "ModelBoardProbeResultPayload",
    "ModelBoardProbeResultRow",
    "ModelBoardProbeResultsProjectionResult",
    "NonEmptyString",
    "VerdictRequest",
]
