# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one iLO 4 Redfish request produced, and the records a write leaves (OMN-21835)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloJournalStatus,
    EnumIloOutcome,
    EnumIloRefusal,
    EnumIloVerb,
)


class ModelIloEvidenceStep(BaseModel):
    """One observation made while serving a request. Redacted before it is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    observed: str


class ModelIloRedfishResult(BaseModel):
    """The typed answer to one request.

    ``data`` holds the redacted body a read verb returns. Its shape is the device's own JSON, so it is
    typed as a JSON value rather than a model per resource. Nothing in this result is a credential, a
    session token or a license key: the redactor runs over every field before construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: EnumIloVerb
    host: str
    outcome: EnumIloOutcome
    refusal: EnumIloRefusal | None = None
    detail: str | None = None
    data: dict[str, JsonValue] = {}
    evidence: tuple[ModelIloEvidenceStep, ...] = ()
    # Present for a write: the key that guards it against repetition.
    idempotency_key: str | None = None
    # Present for a write: one pipe-free line the caller appends to the work ledger.
    ledger_row: str | None = None

    @property
    def sent_nothing_to_change(self) -> bool:
        """True when this result proves no change was sent to the server."""
        return self.outcome in {
            EnumIloOutcome.REFUSED,
            EnumIloOutcome.NOOP,
            EnumIloOutcome.FAILED,
            EnumIloOutcome.ALREADY_ATTEMPTED,
        }


class ModelIloJournalRecord(BaseModel):
    """One line of the per-host action journal: the retry guard of a write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    host: str
    action: str
    status: EnumIloJournalStatus
    at: datetime
    http_status: int | None = None
    detail: str | None = None


class ModelIloApproval(BaseModel):
    """An OPERATOR-CONSENT ledger row, as the approval source read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    at: datetime
    lane: str | None = None
    approved_by: str
    approved_scope: str
    out_of_scope: str
