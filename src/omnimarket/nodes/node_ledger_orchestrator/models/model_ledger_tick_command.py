# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command envelope for the ledger orchestrator's tick input (OMN-8947)."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerTickCommand(BaseModel):
    """Input command for `node_ledger_orchestrator`.

    A `tick_id` is a caller-provided marker used to trace the tick through the
    four-node chain. If omitted, a UUID is generated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: UUID = Field(default_factory=uuid4)


class ModelLedgerAppendCommand(BaseModel):
    """Command emitted by the orchestrator for the append-effect to consume.

    Carries the tick_id forward so the effect writes the correct line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str
    correlation_id: UUID
