# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries for the core-backed work-ledger projection."""

from __future__ import annotations

from typing import Self

from omnibase_core.models.events.work.model_work_ledger_record import (
    ModelWorkLedgerRecord,
)
from omnibase_core.models.nodes.work_ledger_state.model_work_ledger_projection_row import (
    ModelWorkLedgerProjectionRow,
)
from omnibase_core.models.nodes.work_ledger_state.model_work_ledger_state import (
    ModelWorkLedgerState,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.events.model_ledger_row_event import validate_work_ledger_id
from omnimarket.models.model_work_ledger_projection_inbound import (
    ModelWorkLedgerProjectionInbound,
)


class ModelWorkLedgerProjectionRequest(BaseModel):
    """Records already validated by the released core wire contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelWorkLedgerRecord, ...] = ()
    as_of: AwareDatetime | None = None


class ModelWorkLedgerProjectionResult(BaseModel):
    """The pure output: one row per event identity and the core-owned state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelWorkLedgerProjectionRow, ...] = ()
    state: ModelWorkLedgerState


class ModelWorkLedgerProjectionConfig(BaseModel):
    """The configured ledger grain; no consumer-derived default identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ledger_id: str = Field(min_length=1)
    #: Sources whose rows must carry a ledger_seq to be projected (OMN-23488).
    sequence_required_sources: tuple[str, ...]
    #: OMN-24634: the listed sources whose unsequenced rows the projection gives a provisional ledger_seq
    #: (tip+1 under the ledger lock); the judge's own number for that sequence outranks it. Each must also be
    #: in sequence_required_sources.
    sequence_assigned_sources: tuple[str, ...] = ()
    #: The row types work_ledger_latest keeps the newest row of per lane and per ticket (OMN-23606).
    latest_row_types: frozenset[str] = Field(min_length=1)

    _canonical_ledger_id = field_validator("ledger_id")(validate_work_ledger_id)

    @model_validator(mode="after")
    def _assigned_sources_are_required_sources(self) -> Self:
        """A source can only be given a sequence by the projection if it is one that needs one (OMN-24634)."""
        unlisted = sorted(
            set(self.sequence_assigned_sources) - set(self.sequence_required_sources)
        )
        if unlisted:
            raise ValueError(
                f"sequence_assigned_sources {unlisted} are not in sequence_required_sources"
            )
        return self


__all__ = [
    "ModelWorkLedgerProjectionConfig",
    "ModelWorkLedgerProjectionInbound",
    "ModelWorkLedgerProjectionRequest",
    "ModelWorkLedgerProjectionResult",
    "ModelWorkLedgerProjectionRow",
]
