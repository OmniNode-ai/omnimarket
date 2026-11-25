# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows loader's history range: ``--since`` and ``--until`` in place of ``--window-hours``.

A range reads rows older than the 720 hour window through the reader's history read. A range wider
than the store holds is a typed refusal (exit 69, ``range-wider-than-store``), never a cut-short
answer; a range read never falls back to the file.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_effect.runtime_rows_loader import main
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)
from tests.unit.nodes.node_ledger_rows_loader_effect.test_rows_loader_reader_seam import (
    OutagePointRead,
    ledger_file,
    query_node,
)
from tests.unit.nodes.node_ledger_rows_loader_effect.test_runtime_rows_loader import (
    env,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    MSG_ROW,
    SEAM_NOW,
    STATUS_ROWS,
    QueryNodePointRead,
)

pytestmark = pytest.mark.unit

ROWS = (*STATUS_ROWS, MSG_ROW)
SINCE = "2026-11-09T00:00:00Z"


def run(
    tmp_path: Path,
    point_read: ProtocolLedgerPointRead,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    code = main(
        ["--reader", "drain-map", *argv],
        environ=env(tmp_path),
        point_read=point_read,
        clock=lambda: SEAM_NOW,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_since_range_answers_the_rows_from_the_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point = QueryNodePointRead(query_node(ROWS))
    code, out, err = run(tmp_path, point, capsys, "--since", SINCE)
    assert code == 0, err
    assert out.splitlines() == list(ROWS)
    assert point.requests[0].since is not None
    assert point.requests[0].until is None


def test_since_and_until_both_reach_the_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point = QueryNodePointRead(query_node(ROWS))
    code, _, err = run(
        tmp_path, point, capsys, "--since", SINCE, "--until", "2026-11-10T00:00:00Z"
    )
    assert code == 0, err
    assert point.requests[0].until is not None


def test_a_range_wider_than_the_store_is_a_typed_refusal_with_no_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    since = (SEAM_NOW - timedelta(days=4000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, out, err = run(
        tmp_path, QueryNodePointRead(query_node(ROWS)), capsys, "--since", since
    )
    assert code == 69
    assert out == ""
    assert "reason=range-wider-than-store" in err


def test_a_range_read_never_falls_back_to_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = ledger_file(tmp_path, ROWS)
    code, out, err = run(
        tmp_path,
        OutagePointRead(),
        capsys,
        "--since",
        SINCE,
        "--file-ledger",
        str(ledger),
    )
    assert code == 69
    assert out == ""
    assert "source=none" in err


def test_kinds_narrow_a_range_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point = QueryNodePointRead(query_node(ROWS))
    code, out, err = run(tmp_path, point, capsys, "--since", SINCE, "--kind", "MSG")
    assert code == 0, err
    assert out.splitlines() == [MSG_ROW]


@pytest.mark.parametrize(
    "argv",
    [
        ("--since", SINCE, "--window-hours", "27"),
        ("--until", "2026-11-10T00:00:00Z"),
        (),
    ],
)
def test_a_read_names_exactly_one_of_a_window_or_a_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, QueryNodePointRead(query_node(ROWS)), capsys, *argv)
    assert raised.value.code == 2
