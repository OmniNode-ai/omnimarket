# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelChainRecord — a single chain event record for ledger stats computation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelChainRecord(BaseModel):
    """A single completed chain event record.

    Represents the extracted fields from a chain event file. The caller
    (an effect node or test) is responsible for reading and deserialising
    chain files; this node only aggregates.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_passed: bool
    model_id: str = "unknown"
    attempt_count: int = 1
    ledger_hit: bool = False
    has_reference_chains: bool = False


__all__ = ["ModelChainRecord"]
