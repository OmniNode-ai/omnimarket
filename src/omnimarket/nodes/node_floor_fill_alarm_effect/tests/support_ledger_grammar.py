# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger append command the alarm check tests run its rows through: the repository's own row grammar.

The command is ``omnimarket.handlers.ledger_row_grammar.refusal_for_payload`` (the grammar the append path enforces and
``onex-ledger --print-grammar`` prints) behind a script that appends to a file only when the row passes. It also refuses a
row that names a ``principal=``: the ledger writer adds the authenticated principal of the appending host itself and refuses
a row whose own principal differs (read on 2026-11-16 against the bus writer), so a hand-written one is a fault.
"""

from __future__ import annotations

import sys
from pathlib import Path

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    CommandLedger,
)

GRAMMAR_APPENDER = r"""
import sys

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES, refusal_for_payload

target, row = sys.argv[1], sys.argv[2]
refusal = refusal_for_payload(row, sorted(GOVERNED_LEDGER_NAMES)[0])
if refusal is None and any(c.strip().startswith("principal=") for c in row.split(" | ")[2:]):
    refusal = "row principal conflicts with authenticated principal"
if refusal is not None:
    sys.stderr.write(refusal)
    sys.exit(65)
with open(target, "a", encoding="utf-8") as ledger:
    ledger.write(row + "\n")
"""


def grammar_ledger(tmp_path: Path) -> tuple[CommandLedger, Path]:
    """A ledger file written only through the grammar-holding append command."""
    ledger_file = tmp_path / sorted(GOVERNED_LEDGER_NAMES)[0]
    ledger_file.write_text("", encoding="utf-8")
    appender = tmp_path / "grammar_append.py"
    appender.write_text(GRAMMAR_APPENDER, encoding="utf-8")
    return (
        CommandLedger(
            ledger_file, (sys.executable, str(appender), str(ledger_file), "{row}")
        ),
        ledger_file,
    )


__all__: list[str] = ["GRAMMAR_APPENDER", "grammar_ledger"]
