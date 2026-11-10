# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seven readings that prove no file ledger, run as one node, each with the break that turns it red."""

from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers.handler_no_file_readings import (
    HandlerNoFileReadings,
)


class NodeWorkLedgerNoFileReadingsEffect(HandlerNoFileReadings):
    """ONEX entry point."""


__all__ = ["HandlerNoFileReadings", "NodeWorkLedgerNoFileReadingsEffect"]
