# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_work_ledger_seq_gap_effect."""

from __future__ import annotations

from omnimarket.handlers.work_ledger_seq_gap import PostgresWorkLedgerSeqReader
from omnimarket.nodes.node_work_ledger_seq_gap_effect.handlers.handler_work_ledger_seq_gap import (
    HandlerWorkLedgerSeqGap,
)


def create_handler_work_ledger_seq_gap(*, dsn_env: str) -> HandlerWorkLedgerSeqGap:
    """Bind the sequence gap handler to its database reader."""
    return HandlerWorkLedgerSeqGap(PostgresWorkLedgerSeqReader(dsn_env=dsn_env))


__all__ = ["create_handler_work_ledger_seq_gap"]
