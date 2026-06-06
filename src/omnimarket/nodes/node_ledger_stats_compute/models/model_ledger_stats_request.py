# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLedgerStatsRequest — input to the ledger stats compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_ledger_stats_compute.models.model_chain_record import (
    ModelChainRecord,
)


class ModelLedgerStatsRequest(BaseModel):
    """Input for ledger stats computation.

    The caller supplies a list of already-parsed chain records. This node
    performs pure aggregation — no filesystem access, no I/O.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chains: tuple[ModelChainRecord, ...]


__all__ = ["ModelLedgerStatsRequest"]
