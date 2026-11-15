# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file ledger source: reads the ledger file named by the command."""

from datetime import datetime
from pathlib import Path

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.handler_ledger_claims import ModelLedgerRead, read_ledger
from omnimarket.nodes.node_worktree_reconcile_effect.protocols import (
    ProtocolLedgerReader,
)


class FileLedgerReader(ProtocolLedgerReader):
    """Reads the ledger file named by the command; None when the command names no ledger."""

    def read(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> ModelLedgerRead | None:
        if not command.ledger_path:
            return None
        return read_ledger(
            Path(command.ledger_path),
            now,
            quiet_hours=command.policy.quiet_hours,
            max_age_hours=command.policy.ledger_max_age_hours,
            lookback_days=command.policy.ledger_lookback_days,
            max_bytes=command.policy.ledger_max_bytes,
        )
