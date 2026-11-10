# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger row grammar compute node: judges ledger rows against the canonical row grammar."""

from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)


class NodeLedgerRowGrammarCompute(HandlerLedgerRowGrammar):
    """ONEX entrypoint for the ledger row grammar."""


__all__ = ["HandlerLedgerRowGrammar", "NodeLedgerRowGrammarCompute"]
