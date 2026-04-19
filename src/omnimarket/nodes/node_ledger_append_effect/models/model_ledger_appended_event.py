# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-export shim — canonical definition moved to omnimarket.events.ledger (OMN-9263)."""

from omnimarket.events.ledger import (
    ModelLedgerAppendedEvent as ModelLedgerAppendedEvent,
)

__all__ = ["ModelLedgerAppendedEvent"]
