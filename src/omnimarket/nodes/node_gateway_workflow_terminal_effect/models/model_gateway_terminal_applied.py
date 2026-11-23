# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome models of the terminal writer."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnumGatewayTerminalOutcome(StrEnum):
    """What the writer decided for one delivery."""

    APPLIED = "APPLIED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_A_GATEWAY_WORKFLOW = "NOT_A_GATEWAY_WORKFLOW"
    DEAD_LETTERED = "DEAD_LETTERED"


class EnumGatewayTerminalDeadLetterReason(StrEnum):
    """Why a delivery was dead-lettered."""

    MALFORMED_PAYLOAD = "malformed_payload"
    MISSING_MODEL_USED = "missing_model_used"
    AMBIGUOUS_CORRELATION = "ambiguous_correlation"


class EnumGatewayTerminalStoreOutcome(StrEnum):
    """What the store decided inside its one transaction."""

    APPLIED = "APPLIED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NO_ROW = "NO_ROW"
    AMBIGUOUS = "AMBIGUOUS"


class ModelGatewayTerminalApplied(BaseModel):
    """The handler's result for one delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumGatewayTerminalOutcome
    dead_letter_reason: EnumGatewayTerminalDeadLetterReason | None = None
    workflow_id: UUID | None = None


__all__: list[str] = [
    "EnumGatewayTerminalDeadLetterReason",
    "EnumGatewayTerminalOutcome",
    "EnumGatewayTerminalStoreOutcome",
    "ModelGatewayTerminalApplied",
]
