# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for the one ledger read."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_merge_override_alarm_effect.models import ModelLedgerRow


class ProtocolOverrideRowReader(Protocol):
    """Implemented by the Postgres boundary and hermetic test readers."""

    def read_override_rows(
        self, repository: str, pr_number: int
    ) -> Sequence[ModelLedgerRow]:
        """Return candidate RULING rows for the pull request; the matcher compares the head.

        Raises when the source cannot be read.
        """
        ...
