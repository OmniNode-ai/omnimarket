# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator-capture rows reach the ledger as accepted append commands (OMN-16176, plan task L2).

Every capture row carries the ``req=`` and ``via=bus:`` cells of an accepted append, so the
daily parity receipt finds no STATUS row without ``via=``. The handler is the real append
handler over a file-writing command; only the file is a tmp_path.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    accepted_ledger_appender,
    stamp_request_cells,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)

pytestmark = pytest.mark.unit

OPERATOR = "dev-cli-operator"
STATUS = (
    "2026-11-01T20:06:51Z | STATUS | lane=operator-capture | kind=decision | item=cap-1 "
    "| actor=script:handler-capture-process | session=s1 | source=bus:claude "
    '| capture=8b413cb721369442 | said=2026-11-01T20:06:14Z | "Build it now."'
)
RULING = (
    "2026-11-01T20:06:52Z | RULING | lane=operator-capture | ticket=OMN-1 "
    '| question=q | kind=decision | capture=8b413cb721369442 | "Build it now."'
)


_APPEND_SCRIPT = "; ".join(
    (
        "import sys",
        "from pathlib import Path",
        "p = Path(sys.argv[1])",
        "p.write_text(p.read_text() + sys.argv[3] + '\\n')",
    )
)


def _append_command(ledger: Path) -> LocalLedgerAppendCommand:
    return LocalLedgerAppendCommand([sys.executable, "-c", _APPEND_SCRIPT, str(ledger)])


def _handler(ledger: Path) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        _append_command(ledger),
        LocalLedgerFile(ledger),
        "h-ledger",
        writer_principals=["dev-cli-other"],
        operator_principal=OPERATOR,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ROLLING_WORK_LEDGER.md"
    path.write_text("# Ledger\n")
    return path


def _rows(ledger: Path) -> list[str]:
    return ledger.read_text().splitlines()[1:]


def test_a_capture_row_lands_with_req_via_and_the_operator_principal(
    ledger: Path,
) -> None:
    ok, detail = accepted_ledger_appender(
        _handler(ledger), principal=OPERATOR, host_name="h-ledger"
    )(STATUS)
    assert ok, detail
    (row,) = _rows(ledger)
    assert re.search(r"\| req=[0-9a-f-]{36} \|", row)
    assert "| via=bus:capture |" in row
    assert f"principal={OPERATOR}" in row
    assert row.endswith('"Build it now."')


def test_every_row_of_one_prompt_carries_the_same_request_cell(ledger: Path) -> None:
    ok, detail = accepted_ledger_appender(
        _handler(ledger), principal=OPERATOR, host_name="h-ledger"
    )(f"{STATUS}\n{RULING}")
    assert ok, detail
    rows = _rows(ledger)
    assert len(rows) == 2
    cells = [re.search(r"req=([0-9a-f-]{36})", r) for r in rows]
    assert all(cells)
    reqs = {c.group(1) for c in cells if c}
    assert len(reqs) == 1
    assert all("via=bus:capture" in r for r in rows)


def test_a_retried_identical_prompt_is_a_duplicate_not_a_second_row(
    ledger: Path,
) -> None:
    append = accepted_ledger_appender(
        _handler(ledger), principal=OPERATOR, host_name="h-ledger"
    )
    first_ok, _ = append(STATUS)
    assert first_ok
    ok, _ = append(STATUS)
    assert ok
    assert len(_rows(ledger)) == 1


def test_a_refused_append_is_reported_not_swallowed(ledger: Path) -> None:
    ok, detail = accepted_ledger_appender(
        _handler(ledger), principal="unknown-principal", host_name="h-ledger"
    )(STATUS)
    assert not ok
    assert "unknown writer principal" in detail
    assert _rows(ledger) == []


def test_a_ruling_is_refused_from_a_principal_that_is_not_the_operator(
    ledger: Path,
) -> None:
    ok, detail = accepted_ledger_appender(
        _handler(ledger), principal="dev-cli-other", host_name="h-ledger"
    )(RULING)
    assert not ok
    assert "operator principal" in detail


def test_the_stamp_puts_the_cells_before_free_text_and_after_key_cells() -> None:
    stamped = stamp_request_cells(
        f"{STATUS}\n2026-11-01T20:07:00Z | STATUS | lane=x | n=1", "abc"
    )
    first, second = stamped.splitlines()
    assert '| req=abc | via=bus:capture | "Build it now."' in first
    assert second.endswith("| n=1 | req=abc | via=bus:capture")
