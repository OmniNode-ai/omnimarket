# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared ledger event models — canonical home for cross-node ledger event contracts.

These models are consumed by multiple ledger nodes (node_ledger_append_effect,
node_ledger_hash_compute, node_ledger_state_reducer) and must not live inside
any single node's package to avoid cross-node reach-in violations.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerAppendedEvent(BaseModel):
    """Emitted after the effect writes a tick line to the journal."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str
    correlation_id: UUID
    line_number: int
    line_content: str


class ModelLedgerHashComputed(BaseModel):
    """Pure compute result: sha256 of the journal at the moment of compute."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str
    correlation_id: UUID
    line_count: int
    sha256_hex: str = Field(description="64-char hex digest of journal bytes")
