# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the review lock: the ledger read and the ledger write."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    ModelStatusRow,
)


class ProtocolLedgerStatusReader(Protocol):
    """Reads the STATUS rows of one repository, oldest first (archive rolls, then the live file)."""

    def status_rows(self, repo: str) -> Sequence[ModelStatusRow]: ...


class ProtocolLedgerRowWriter(Protocol):
    """Appends one row through the ledger writer; raises OSError when it refuses."""

    def append(self, row: str) -> None: ...
