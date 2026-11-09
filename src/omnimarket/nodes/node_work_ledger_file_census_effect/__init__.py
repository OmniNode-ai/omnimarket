# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census of executable readers and writers of the ledger file, held by a shrink-only ratchet."""

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)


class NodeWorkLedgerFileCensusEffect(HandlerFileCensus):
    """ONEX entry point."""


__all__ = ["HandlerFileCensus", "NodeWorkLedgerFileCensusEffect"]
