# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Markdown work-ledger projection: row events to one rendered file, in ledger_seq order."""

from omnimarket.nodes.node_work_ledger_render_effect.handlers import (
    HandlerWorkLedgerRender,
)


class NodeWorkLedgerRenderEffect(HandlerWorkLedgerRender):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerRender", "NodeWorkLedgerRenderEffect"]
