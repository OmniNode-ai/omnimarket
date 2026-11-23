# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger append command the alarm check tests run its rows through: the real work ledger append path.

The command is a script that stamps the row with its request cell the way the append command does
(``stamp_request_cell``), judges it with the repository's own row grammar (``refusal_for_payload``, the check of the append
path) and hands it to ``HandlerWorkLedgerAppendEffect``, the handler the ledger host's writer runs, as one append request
from the authenticated principal. The script holds no copy of any rule: a row that is refused exits 65 with the refusal, and
a row that is accepted is appended with the principal the handler checked (or stamped) on it. A row naming a principal other
than the authenticated one is refused by the writer's own check, and a row with none is stamped.
"""

from __future__ import annotations

import sys
from pathlib import Path

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    CommandLedger,
)

AUTHENTICATED_PRINCIPAL = "floor-fill-alarm-host"

REAL_WRITER_APPENDER = r"""
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.ledger_row_grammar import refusal_for_payload
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    stamp_request_cell,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)


class Runner:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, rows: str) -> ModelAppendCommandResult:
        with self.path.open("a", encoding="utf-8") as ledger:
            ledger.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0, stderr="")


target, principal, row = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
handler = HandlerWorkLedgerAppendEffect(
    Runner(target),
    LocalLedgerFile(target),
    "ledger",
    writer_principals=(principal,),
    operator_principal="operator",
)
request_id = uuid.uuid4()
stamped = stamp_request_cell(row, str(request_id))
refusal = refusal_for_payload(stamped, target.name)
if refusal is not None:
    sys.stderr.write(refusal)
    sys.exit(65)
receipt = handler.handle(
    ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=stamped,
        requested_by_lane="floor-fill-alarm",
        requesting_host="alarm-host",
        requested_at=datetime.now(UTC),
    ),
    writer_principal=principal,
)
if receipt.status is not EnumWorkLedgerAppendStatus.ACCEPTED:
    sys.stderr.write(f"{receipt.status}: {receipt.message}")
    sys.exit(65)
"""


def real_writer_ledger(
    tmp_path: Path, principal: str = AUTHENTICATED_PRINCIPAL
) -> tuple[CommandLedger, Path]:
    """A ledger file written only through the real append handler, as the authenticated ``principal``."""
    ledger_file = tmp_path / sorted(GOVERNED_LEDGER_NAMES)[0]
    ledger_file.write_text("", encoding="utf-8")
    appender = tmp_path / "real_writer_append.py"
    appender.write_text(REAL_WRITER_APPENDER, encoding="utf-8")
    return (
        CommandLedger(
            ledger_file,
            (sys.executable, str(appender), str(ledger_file), principal, "{row}"),
        ),
        ledger_file,
    )


__all__: list[str] = [
    "AUTHENTICATED_PRINCIPAL",
    "REAL_WRITER_APPENDER",
    "real_writer_ledger",
]
