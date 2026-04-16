# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event emitted by `node_ledger_append_effect` after journal append (OMN-8948)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelLedgerAppendedEvent(BaseModel):
    """Emitted after the effect writes a tick line to the journal."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str
    correlation_id: UUID
    line_number: int
    line_content: str
