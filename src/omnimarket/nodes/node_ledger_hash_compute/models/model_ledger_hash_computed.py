# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for `node_ledger_hash_compute` (OMN-8949)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerHashComputed(BaseModel):
    """Pure compute result: sha256 of the journal at the moment of compute."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_id: str
    correlation_id: UUID
    line_count: int
    sha256_hex: str = Field(description="64-char hex digest of journal bytes")
