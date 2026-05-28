# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLedgerStats — output of the ledger stats compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelModelStats(BaseModel):
    """Per-model aggregated stats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: int
    failed: int
    avg_attempts: float


class ModelLedgerStats(BaseModel):
    """Aggregated ledger chain statistics.

    Returned by HandlerLedgerStats after processing a batch of chain records.
    All counts are non-negative integers; ledger_hit_rate is in [0.0, 1.0].
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_chains: int
    pass_count: int
    fail_count: int
    by_model: dict[str, ModelModelStats]
    ledger_hit_rate: float


__all__ = ["ModelLedgerStats", "ModelModelStats"]
