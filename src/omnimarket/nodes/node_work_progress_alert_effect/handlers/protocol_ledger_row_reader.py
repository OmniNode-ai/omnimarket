# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for the one ledger read."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_work_progress_alert_effect.models import ModelLedgerRow


class ProtocolLedgerRowReader(Protocol):
    """Implemented by the Postgres boundary and hermetic test readers."""

    def read(self, claimed_since: datetime) -> Sequence[ModelLedgerRow]:
        """Return CLAIM, STATUS and TERMINAL rows of every lane with a CLAIM at or after the cutoff.

        Raises when the source cannot be read.
        """
        ...
