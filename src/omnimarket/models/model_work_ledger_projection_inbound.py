# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared wire envelope for core-backed work-ledger events.

Both the work-ledger producers and the work-ledger projection read this
envelope, so it lives in the shared models package rather than in either node.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnibase_core.models.events.work.model_work_event_union import ModelWorkEvent
from omnibase_core.models.events.work.model_work_ledger_record import (
    WORK_LEDGER_SCHEMA,
    ModelWorkLedgerRecord,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.events.model_ledger_row_event import (
    validate_work_ledger_id,
    work_ledger_event_id,
    work_ledger_row_id,
)


class ModelWorkLedgerProjectionInbound(BaseModel):
    """The stage-2 wire envelope around the released core event body.

    ``raw_row`` and ``row_id`` retain the original normalized source provenance during
    the transition, while the core event remains the only typed work-event
    definition.  The writer converts this envelope to the canonical JSONL
    record consumed by the pure fold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str = Field(min_length=1)

    _canonical_ledger_id = field_validator("ledger_id")(validate_work_ledger_id)
    event_id: UUID
    row_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1)
    raw_row: str = Field(min_length=1)
    schema_version: Literal["1.0.0"] = "1.0.0"
    row_schema: Literal["work-ledger-event/2"] = "work-ledger-event/2"
    provenance_kind: Literal["markdown", "typed"]
    correlation_id: str | None = None
    causation_id: str | None = None
    emitted_at: AwareDatetime | None = None
    session_id: str | None = None
    entity_id: str | None = None
    event: ModelWorkEvent

    @model_validator(mode="after")
    def _event_identity_matches_body(self) -> ModelWorkLedgerProjectionInbound:
        if self.event_id != self.event.event_id:
            raise ValueError("wire event_id must equal the core event event_id")
        if self.raw_row != self.raw_row.strip():
            raise ValueError("raw_row must preserve the normalized source row")
        if work_ledger_row_id(self.raw_row) != self.row_id:
            raise ValueError("row_id does not match normalized source row")
        if self.provenance_kind == "markdown" and self.event_id != work_ledger_event_id(
            self.ledger_id, self.row_id
        ):
            raise ValueError(
                "markdown event_id must match the canonical UUID5 identity"
            )
        return self

    def to_record(self) -> ModelWorkLedgerRecord:
        """Return the canonical JSON-lines record for the core fold."""
        return ModelWorkLedgerRecord.model_validate(
            {"schema": WORK_LEDGER_SCHEMA, "event": self.event}
        )


__all__ = ["ModelWorkLedgerProjectionInbound"]
