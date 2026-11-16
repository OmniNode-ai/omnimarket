# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The append handler stamps ``via=bus:<requesting host>`` on every accepted row (OMN-24205).

About a thousand STATUS rows a day come from scheduled node writers that send ``req=`` and no bus stamp; the
plan's exact-day bar (L2 of the work-ledger cutover) needs every row stamped.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    stamp_request_cell,
)
from omnimarket.nodes.node_work_ledger_append_effect import EnumWorkLedgerAppendStatus
from omnimarket.nodes.node_work_ledger_append_effect.protocols import LocalLedgerFile
from tests.unit.nodes.node_work_ledger_append_effect.test_handler_work_ledger_append_effect import (
    _attributed,
    _handler,
    _ledger,
    _request,
    _row,
    _Runner,
)

pytestmark = pytest.mark.unit

_KINDS = (
    "CLAIM",
    "STATUS",
    "TERMINAL",
    "HOLD",
    "RELEASE",
    "MSG",
    "ACK",
    "FRICTION",
    "CORRECTION",
)


def test_stamps_every_kind(tmp_path: Path) -> None:
    """Golden: a row ending in the request cell, the node writers' shape, gains the stamp after it."""
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    rows = "\n".join(
        f"2026-10-01T12:00:00Z | {kind} | lane=test | req={request_id}"
        for kind in _KINDS
    )
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    lines = runner.calls[0].splitlines()
    assert len(lines) == len(_KINDS)
    for kind, line in zip(_KINDS, lines, strict=True):
        assert line.endswith(f" | req={request_id} | via=bus:lab-host"), line
        assert f" | {kind} | " in line
        assert line.count("via=bus:") == 1


def test_stamps_after_the_request_cell_when_text_follows(tmp_path: Path) -> None:
    """The stamp sits right after req=, ahead of free text, as the client writer places it."""
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(_row(request_id), request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert f" | req={request_id} | via=bus:lab-host | exact text" in runner.calls[0]


def test_keeps_an_existing_stamp(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    row = _row(request_id).replace(
        f"req={request_id} | exact text",
        f"req={request_id} | via=bus:other | exact text",
    )
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(row, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert " | via=bus:other | " in runner.calls[0]
    assert runner.calls[0].count("via=bus:") == 1


def test_redelivery_of_an_unstamped_request(tmp_path: Path) -> None:
    """A request that landed before this change (no stamp) answers duplicate, never a refusal."""
    path = _ledger(tmp_path)
    request_id = uuid4()
    row = _row(request_id)
    old_rendering = _attributed(row).replace(" | via=bus:lab-host", "")
    assert "via=bus:" not in old_rendering
    path.write_text(
        path.read_text(encoding="utf-8") + old_rendering + "\n", encoding="utf-8"
    )
    runner = _Runner(path)
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(row, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert runner.calls == []


def test_redelivery_with_different_text_is_still_refused(tmp_path: Path) -> None:
    """Failing control: accepting the earlier renderings must not accept different rows."""
    path = _ledger(tmp_path)
    request_id = uuid4()
    old_rendering = _attributed(_row(request_id)).replace(" | via=bus:lab-host", "")
    path.write_text(
        path.read_text(encoding="utf-8") + old_rendering + "\n", encoding="utf-8"
    )
    runner = _Runner(path)
    changed = _row(request_id).replace("exact text", "other text")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(changed, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert runner.calls == []


def test_refuses_a_host_that_cannot_be_stamped(tmp_path: Path) -> None:
    """Error chain: a host name that would split the row cannot become a via cell."""
    path = _ledger(tmp_path)
    before = path.read_bytes()
    runner = _Runner(path)
    request_id = uuid4()
    request = _request(_row(request_id), request_id).model_copy(
        update={"requesting_host": "bad host | x"}
    )
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        request, writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65
    assert "requesting_host cannot be stamped as a via cell" in receipt.message
    assert runner.calls == []
    assert path.read_bytes() == before


def test_seam_with_the_node_writer(tmp_path: Path) -> None:
    """The node writer's own request shape, through the real handler, reads back through the row grammar."""
    path = _ledger(tmp_path)
    runner = _Runner(path)
    request_id = uuid4()
    node_row = "2026-10-01T12:00:00Z | STATUS | lane=reading-writer | src=reading | verdict=WORKING\n"
    rows = stamp_request_cell(node_row, str(request_id))
    assert rows.rstrip("\n").endswith(f" | req={request_id}")
    receipt = _handler(runner, LocalLedgerFile(path), "ledger").handle(
        _request(rows, request_id), writer_principal="developer"
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    parsed = parse_row(runner.calls[0].splitlines()[0])
    assert parsed is not None
    assert parsed.fields["via"] == ["bus:lab-host"]
    assert parsed.fields["req"] == [str(request_id)]
    assert parsed.fields["principal"] == ["developer"]
