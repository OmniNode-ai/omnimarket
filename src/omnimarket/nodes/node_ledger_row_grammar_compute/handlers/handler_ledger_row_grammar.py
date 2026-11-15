# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decisions of the ledger row grammar.

The grammar itself, and its helpers, are shared handlers (``omnimarket.handlers.ledger_row_grammar``)
so the nodes that append or admit ledger rows can call one source. This handler is the node's
question-and-answer surface over it: it reads the environment variables and paths the grammar's
worktree and delegation-receipt rules read, and nothing else; it writes nothing.
"""

from __future__ import annotations

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.handlers import ledger_running_path_cell as running_path_cell
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
    ModelLedgerRowGrammarResult,
)


class HandlerLedgerRowGrammar:
    """Stateless: judge a row, a payload or a payload against the written ledger, answer a
    payload's running-path share, or print the grammar."""

    def handle(
        self, request: ModelLedgerRowGrammarRequest
    ) -> ModelLedgerRowGrammarResult:
        version = grammar.GRAMMAR_VERSION
        if request.operation == "row_refusals":
            refusals = grammar.row_refusals(request.line)
            return ModelLedgerRowGrammarResult(
                operation=request.operation,
                refusals=refusals,
                admitted=not refusals,
                grammar_version=version,
            )
        if request.operation == "running_path_share":
            confirming = 0
            carrying = 0
            for line in request.payload.splitlines():
                if not grammar.opens_a_row(line):
                    continue
                row = grammar.parse_row(line)
                if row is None or row.row_type != "TERMINAL":
                    continue
                # The outcome is read where the rule reads it: any pipe cell that declares it.
                if not any(
                    value.strip().lower() in running_path_cell.CONFIRMING_OUTCOMES
                    for value in running_path_cell.declared_values(line)["outcome"]
                ):
                    continue
                if grammar.governed_terminal_row(line) is None:
                    continue
                confirming += 1
                if (
                    grammar.running_path_refusal(
                        line, running_path_cell.EVERY_ROW_STAMP
                    )
                    is None
                ):
                    carrying += 1
            return ModelLedgerRowGrammarResult(
                operation=request.operation,
                admitted=True,
                grammar_version=version,
                confirming_rows=confirming,
                carrying_rows=carrying,
                share=carrying / confirming if confirming else None,
            )
        if request.operation == "payload_refusal":
            refusal = grammar.refusal_for_payload(request.payload, request.ledger_name)
        elif request.operation == "state_refusal":
            refusal = grammar.refusal_for_state(
                request.payload, request.existing, request.ledger_name
            )
        else:
            return ModelLedgerRowGrammarResult(
                operation=request.operation,
                admitted=True,
                grammar_version=version,
                grammar_json=grammar.grammar_json(),
            )
        return ModelLedgerRowGrammarResult(
            operation=request.operation,
            refusal=refusal,
            admitted=refusal is None,
            grammar_version=version,
        )
