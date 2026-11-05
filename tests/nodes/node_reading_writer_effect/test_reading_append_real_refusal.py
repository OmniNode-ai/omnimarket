# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading appender against the real work-ledger append handler (OMN-22478).

The first hourly run on h051 had all 18 reading rows refused: the real append handler answers
``row 1 (STATUS) must carry the whole pipe cell req=<request id>`` and the appender sent rows
with no such cell. The appender then reported the stderr tail instead of the receipt on stdout,
which hid that reason. The command used here is the real handler, not a double that accepts
anything: a script that builds ``HandlerWorkLedgerAppendEffect`` and prints its receipt as the
bus append command does.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingComputeResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters import ledger_adapters
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    build_reading_row,
)
from tests.nodes.node_reading_writer_effect.reading_support import RUN_AT

pytestmark = pytest.mark.unit

REQUEST_ID = "6f2c1c0e-7d52-5a3e-9b0b-2f7c1d0a4e11"

_REAL_HANDLER = """
import sys
from datetime import UTC, datetime
from uuid import UUID

from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)


class Ledger:
    text = "# Rolling ledger\\n"

    def read_text(self):
        return Ledger.text

    def append(self, rows):
        Ledger.text += rows + "\\n"
        return ModelAppendCommandResult(exit_code=0)


request_id = sys.argv[sys.argv.index("--request-id") + 1]
rows = sys.stdin.read()
ledger = Ledger()
handler = HandlerWorkLedgerAppendEffect(
    ledger, ledger, "ledger-host", writer_principals=("developer",), operator_principal="operator"
)
receipt = handler.handle(
    ModelWorkLedgerAppendRequest(
        request_id=UUID(request_id),
        rows=rows,
        requested_by_lane="reading-writer",
        requesting_host="h-test",
        requested_at=datetime.now(UTC),
    ),
    writer_principal={principal},
)
print("bus-binding: diagnostics only", file=sys.stderr)
print(receipt.model_dump_json())
sys.exit(receipt.exit_code)
"""


def _script(principal: str | None = "developer") -> str:
    return _REAL_HANDLER.replace("{principal}", repr(principal))


def _reading_row() -> str:
    result = ModelReadingComputeResult(
        piece=6,
        name="ledger append and projection",
        verdict=EnumReadingVerdict.UNKNOWN,
        outcome=EnumReadingOutcome.READ,
        reasons=("no-producer:test",),
        window_start=RUN_AT,
        window_end=RUN_AT,
        order_hash="a" * 64,
        overlay_hash="b" * 64,
        revision="test",
        manifest_hash="c" * 64,
        rows_read=0,
        row_key="k" * 24,
        content_hash="d" * 24,
        injected_fault="none",
    )
    return build_reading_row(
        result,
        lane="reading-writer",
        actor="node:node_reading_writer_effect",
        published_at=RUN_AT,
        probed_at=RUN_AT,
        max_age_seconds=7200,
    )


def _real_append(rows: str, principal: str | None = "developer") -> dict[str, object]:
    done = subprocess.run(
        [sys.executable, "-c", _script(principal), "--request-id", REQUEST_ID],
        input=rows,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    receipt = json.loads(done.stdout.strip().splitlines()[-1])
    receipt["exit"] = done.returncode
    return receipt


def test_the_real_handler_refuses_the_row_shape_the_writer_used_to_send() -> None:
    receipt = _real_append(_reading_row() + "\n")
    assert receipt["status"] == "refused"
    assert receipt["exit"] == 65
    assert (
        f"row 1 (STATUS) must carry the whole pipe cell req={REQUEST_ID}"
        in receipt["message"]
    )


def test_the_command_appender_lands_a_reading_row_the_real_handler_accepts() -> None:
    appender = CommandLedgerAppender(
        [sys.executable, "-c", _script()], "reading-writer", "h-test", 60
    )
    outcome = appender.append(_reading_row() + "\n", REQUEST_ID)
    assert outcome.status == "accepted", outcome.message


def test_a_refusal_reports_the_receipt_on_stdout_not_the_stderr_tail() -> None:
    appender = CommandLedgerAppender(
        [sys.executable, "-c", _script(principal=None)], "reading-writer", "h-test", 60
    )
    outcome = appender.append(_reading_row() + "\n", REQUEST_ID)
    assert outcome.status == "refused"
    assert "unattributed" in outcome.message
    assert "diagnostics only" not in outcome.message


def test_a_row_without_the_request_cell_is_refused_with_the_real_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failing control: the appender as it was, sending rows with no req= cell.
    monkeypatch.setattr(
        ledger_adapters,
        "stamp_request_cell",
        lambda rows, _request_id: rows,
        raising=False,
    )
    appender = CommandLedgerAppender(
        [sys.executable, "-c", _script()], "reading-writer", "h-test", 60
    )
    outcome = appender.append(_reading_row() + "\n", REQUEST_ID)
    assert outcome.status == "refused"
    assert f"must carry the whole pipe cell req={REQUEST_ID}" in outcome.message


def test_the_request_cell_goes_on_every_row_once_and_never_on_a_continuation() -> None:
    stamp_request_cell = ledger_adapters.stamp_request_cell
    row = _reading_row()
    once = stamp_request_cell(row + "\n  continued text\n" + row + "\n", REQUEST_ID)
    lines = once.splitlines()
    assert lines[0].endswith(f" | req={REQUEST_ID}")
    assert lines[1] == "  continued text"
    assert lines[2].endswith(f" | req={REQUEST_ID}")
    assert stamp_request_cell(once, REQUEST_ID) == once
