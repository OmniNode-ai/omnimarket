# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed config and answer of the pin friction ledger effect (OMN-20134)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.receipt_gate_pin_friction import EnumPinFrictionReason


class EnumPinFrictionLedgerStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"
    ERROR = "error"
    PENDING = "pending"


class ModelPinFrictionLedgerConfig(BaseModel):
    """``pin_friction_ledger_config`` of this node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    ticket: str = Field(pattern=r"^OMN-\d+$")
    friction_class: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    actor: str = Field(min_length=1)
    cost_by_reason: dict[EnumPinFrictionReason, str] = Field(
        description="The cost cell of each friction reason, an estimate in lane-minutes."
    )
    ledger_bus_lane_env: str = Field(min_length=1)
    workspace_root_env: str = Field(min_length=1)
    request_namespace: UUID = Field(
        description="Namespace of the uuid5 request id, so a redelivered event is the same ledger request."
    )
    timeout_s: float = Field(gt=0)


class ModelPinFrictionLedgerAppended(BaseModel):
    """Exactly one per friction event, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    status: EnumPinFrictionLedgerStatus
    row: str
    ledger_lines: tuple[int, ...] = ()
    ledger_host: str = ""
    message: str = Field(default="", max_length=2000)
    answered_at: Annotated[AwareDatetime, Field()]


__all__ = [
    "EnumPinFrictionLedgerStatus",
    "ModelPinFrictionLedgerAppended",
    "ModelPinFrictionLedgerConfig",
]
