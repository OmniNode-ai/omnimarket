# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The error a ledger-rows read raises when the rows loader did not answer."""

from __future__ import annotations


class LedgerRowsUnreadableError(RuntimeError):
    """The rows loader did not answer: the caller reads nothing in its place."""
