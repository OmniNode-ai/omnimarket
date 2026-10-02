# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries for the core-backed work-ledger projection."""

from __future__ import annotations

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

    _canonical_ledger_id = field_validator("ledger_id")(validate_work_ledger_id)


__all__ = [
    "ModelWorkLedgerProjectionConfig",
    "ModelWorkLedgerProjectionInbound",
    "ModelWorkLedgerProjectionRequest",
    "ModelWorkLedgerProjectionResult",
    "ModelWorkLedgerProjectionRow",
]
