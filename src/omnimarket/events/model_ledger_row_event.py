# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed events for the eleven rolling-ledger row types (OMN-19513).

Every event carries the exact raw row, its content hash and its parsed cells,
so a reader can rebuild the markdown row from the event and the parity check
compares identical strings. Each type adds the typed fields the grammar gives
it meaning through; nothing here judges a row (the append path already did).

``raw_row`` is the truth. The typed fields are derived from it by the pure
parser, and every consumer re-derives them (the projection reparses ``raw_row``),
so an emitter that cannot afford the parser -- the ``onex-ledger`` dual write
runs stdlib-only -- publishes only the envelope fields and stays correct. The
lane travels as ``row_lane``, never ``lane``: the hook journal writer stamps its
own ``lane`` attribution onto every payload it appends.

The partition key of every event is ``ledger_id``: one constant per ledger puts
the whole ledger on one partition, so the fold's order is the partition offset
and never a clock.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)

SCHEMA_VERSION = "work-ledger-event/1"
DEFAULT_LEDGER_ID = "rolling-work-ledger"
WORK_LEDGER_EVENT_NAMESPACE = UUID("44cfd495-27b1-5bd6-960b-540950a90475")


def work_ledger_row_id(raw_row: str) -> str:
    """Hash the source preimage; never the subsequently rendered typed view."""
    return hashlib.sha256(raw_row.strip().encode("utf-8")).hexdigest()


def validate_work_ledger_id(ledger_id: str) -> str:
    """Validate the contract's canonical ledger ID without rewriting it."""
    if not ledger_id or ledger_id != ledger_id.strip():
        raise ValueError("ledger_id must be a nonblank canonical ledger identifier")
    return ledger_id


def work_ledger_event_id(ledger_id: str, row_id: str) -> UUID:
    """The single producer-owned UUID5 identity algorithm (KB-internal#895)."""
    validate_work_ledger_id(ledger_id)
    if not re.fullmatch(r"[0-9a-f]{64}", row_id):
        raise ValueError("row_id must be a lowercase SHA256 hash")
    return uuid5(uuid5(WORK_LEDGER_EVENT_NAMESPACE, ledger_id), row_id)


_STAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelLedgerCell(BaseModel):
    """One `key=value` pipe cell of a row, in row order (keys may repeat)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., min_length=1)
    value: str


class ModelLedgerRowEventBase(BaseModel):
    """Fields common to all eleven events."""

    # ``extra="ignore"``: the emit path injects standard envelope metadata into
    # the payload dict before it reaches a consumer; the fold must tolerate it.
    model_config = ConfigDict(frozen=True, extra="ignore")

    ledger_id: str = Field(default=DEFAULT_LEDGER_ID, min_length=1)
    row_type: EnumLedgerRowType
    row_id: str = Field(..., pattern=r"^[0-9a-f]{64}$", description="sha256 of raw_row")
    row_timestamp: str = Field(..., pattern=_STAMP_PATTERN)
    row_lane: str | None = None
    tickets: tuple[str, ...] = ()
    cells: tuple[ModelLedgerCell, ...] = ()
    free_text: str = ""
    raw_row: str = Field(..., min_length=1)
    source: str = Field(default="onex-ledger", min_length=1)
    row_schema: str = SCHEMA_VERSION


class ModelLedgerClaimEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.CLAIM] = EnumLedgerRowType.CLAIM
    repo: str | None = None
    pr: str | None = None
    parent: str | None = None
    consent: str | None = None


class ModelLedgerStatusEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.STATUS] = EnumLedgerRowType.STATUS
    repo: str | None = None
    pr: str | None = None
    head: str | None = None
    state: str | None = None
    claim: str | None = None


class ModelLedgerTerminalEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.TERMINAL] = EnumLedgerRowType.TERMINAL
    outcome: str | None = None
    friction: str | None = None
    pr: str | None = None
    closes: str | None = None


class ModelLedgerHoldEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.HOLD] = EnumLedgerRowType.HOLD
    hold_id: str | None = None
    scope_to: str | None = None
    scope_repo: str | None = None
    scope_pr: str | None = None
    scope_surface: str | None = None
    until: str | None = None
    after: str | None = None
    release_condition: str | None = None


class ModelLedgerReleaseEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.RELEASE] = EnumLedgerRowType.RELEASE
    re: str | None = None
    surface: str | None = None
    result: str | None = None


class ModelLedgerMsgEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.MSG] = EnumLedgerRowType.MSG
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    msg_id: str | None = None
    re: str | None = None
    repo: str | None = None
    pr: str | None = None


class ModelLedgerAckEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.ACK] = EnumLedgerRowType.ACK
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    msg_id: str | None = None
    re: str | None = None


class ModelLedgerRulingEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.RULING] = EnumLedgerRowType.RULING
    amends: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    approved_by: str | None = None


class ModelLedgerOperatorConsentEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.OPERATOR_CONSENT] = (
        EnumLedgerRowType.OPERATOR_CONSENT
    )
    approved_scope: str | None = None
    out_of_scope: str | None = None
    approved_by: str | None = None


class ModelLedgerFrictionEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.FRICTION] = EnumLedgerRowType.FRICTION
    cost: str | None = None
    friction_class: str | None = None


class ModelLedgerCorrectionEvent(ModelLedgerRowEventBase):
    row_type: Literal[EnumLedgerRowType.CORRECTION] = EnumLedgerRowType.CORRECTION
    corrects: tuple[str, ...] = ()


ModelLedgerRowEvent = Annotated[
    ModelLedgerClaimEvent
    | ModelLedgerStatusEvent
    | ModelLedgerTerminalEvent
    | ModelLedgerHoldEvent
    | ModelLedgerReleaseEvent
    | ModelLedgerMsgEvent
    | ModelLedgerAckEvent
    | ModelLedgerRulingEvent
    | ModelLedgerOperatorConsentEvent
    | ModelLedgerFrictionEvent
    | ModelLedgerCorrectionEvent,
    Field(discriminator="row_type"),
]

EVENT_MODEL_BY_TYPE: dict[EnumLedgerRowType, type[ModelLedgerRowEventBase]] = {
    EnumLedgerRowType.CLAIM: ModelLedgerClaimEvent,
    EnumLedgerRowType.STATUS: ModelLedgerStatusEvent,
    EnumLedgerRowType.TERMINAL: ModelLedgerTerminalEvent,
    EnumLedgerRowType.HOLD: ModelLedgerHoldEvent,
    EnumLedgerRowType.RELEASE: ModelLedgerReleaseEvent,
    EnumLedgerRowType.MSG: ModelLedgerMsgEvent,
    EnumLedgerRowType.ACK: ModelLedgerAckEvent,
    EnumLedgerRowType.RULING: ModelLedgerRulingEvent,
    EnumLedgerRowType.OPERATOR_CONSENT: ModelLedgerOperatorConsentEvent,
    EnumLedgerRowType.FRICTION: ModelLedgerFrictionEvent,
    EnumLedgerRowType.CORRECTION: ModelLedgerCorrectionEvent,
}

__all__: list[str] = [
    "DEFAULT_LEDGER_ID",
    "EVENT_MODEL_BY_TYPE",
    "SCHEMA_VERSION",
    "ModelLedgerAckEvent",
    "ModelLedgerCell",
    "ModelLedgerClaimEvent",
    "ModelLedgerCorrectionEvent",
    "ModelLedgerFrictionEvent",
    "ModelLedgerHoldEvent",
    "ModelLedgerMsgEvent",
    "ModelLedgerOperatorConsentEvent",
    "ModelLedgerReleaseEvent",
    "ModelLedgerRowEvent",
    "ModelLedgerRowEventBase",
    "ModelLedgerRulingEvent",
    "ModelLedgerStatusEvent",
    "ModelLedgerTerminalEvent",
]
