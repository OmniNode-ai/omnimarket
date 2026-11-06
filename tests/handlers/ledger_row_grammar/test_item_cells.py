# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Item linkage is admitted and documented by the existing ledger grammar node."""

import json

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("kind", "cells"),
    [
        ("STATUS", "lane=orch"),
        (
            "CLAIM",
            "lane=builder | actor=codex | ticket=OMN-1 | worktree=none | est ~1 lane-hours; displaces idle; (OMN-1)",
        ),
        ("MSG", "from=orch | to=operator | id=2026-11-04T12:00:00Z-orch"),
    ],
)
def test_item_cells_append_to_scratch(kind: str, cells: str, tmp_path) -> None:
    row = f"2026-11-04T12:00:00Z | {kind} | {cells} | for-item=ask-a | owner=orch | answers=message-a | item=cap-a | recorded"
    result = HandlerLedgerRowGrammar().handle(
        ModelLedgerRowGrammarRequest(operation="row_refusals", line=row)
    )
    assert result.admitted, result.refusals
    ledger = tmp_path / "ledger.md"
    ledger.write_text(row + "\n")
    assert (
        grammar.refusal_for_payload(ledger.read_text(), "ROLLING_WORK_LEDGER.md")
        is None
    )
    spec = json.loads(grammar.grammar_json())
    assert spec


@pytest.mark.parametrize("kind", ["STATUS", "CLAIM", "MSG"])
def test_item_cells_are_in_printed_grammar(kind: str) -> None:
    spec = json.loads(grammar.grammar_json())
    assert {"for-item", "owner", "answers", "item"} <= set(
        spec["types"][kind]["optional"]
    )
