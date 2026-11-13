# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hold verdict CLI end to end on the in-memory path (OMN-19609, cutover plan step L6).

HELD (exit 3) from a hold only in the projection; CLEAR (exit 0) from the node; a node fault with
no file lines is UNDECIDED (exit 2); a file fallback is CLEAR with ``source=file-fallback`` on
stderr; a retired fallback is UNDECIDED even with file lines. The journal receives one event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_hold_verdict_effect.runtime_hold_verdict import main
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from tests.unit.nodes.node_ledger_hold_verdict_effect.test_hold_verdict_reader_seam import (
    pr_hold_entity,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    FakeStateReader,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
)

pytestmark = pytest.mark.unit

READER_OVERLAY = """\
moved_readers: [pause-check]
max_watermark_age_seconds: 900
page_size: 200
max_pages: 50
route: {kind: inmemory}
parity: {receipt_lane: work-ledger-parity, window_hours: 48}
events: {journal_env: ONEX_LEDGER_READER_JOURNAL}
"""


def env(tmp_path: Path, *, fallback: bool = True) -> dict[str, str]:
    (tmp_path / "reader.yaml").write_text(READER_OVERLAY)
    (tmp_path / "verdict.yaml").write_text(
        f"file_fallback_allowed: {str(fallback).lower()}\n"
        "journal_env: ONEX_LEDGER_HOLD_VERDICT_JOURNAL\n"
    )
    return {
        "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
        "ONEX_LEDGER_HOLD_VERDICT_OVERLAY": str(tmp_path / "verdict.yaml"),
        "ONEX_LEDGER_HOLD_VERDICT_JOURNAL": str(tmp_path / "journal.jsonl"),
    }


def run(
    tmp_path: Path,
    state: FakeStateReader,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    fallback: bool = True,
    pr: str = "omnimarket#7",
) -> tuple[int, str, str]:
    reader = QueryNodePointRead(state)
    # The handlers read the real clock; the state page's watermark is fixed, so the read is stale
    # against the real clock. Pin the freshness bound wide in the overlay instead of the clock.
    environment = env(tmp_path, fallback=fallback)
    (tmp_path / "reader.yaml").write_text(
        READER_OVERLAY.replace(
            "max_watermark_age_seconds: 900", "max_watermark_age_seconds: 99999999"
        )
    )
    code = main(
        ["--pr", pr, "--lane", "lane-x", *extra],
        environ=environment,
        point_read=reader,
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def test_a_hold_only_in_the_projection_exits_3_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(tmp_path, FakeStateReader((pr_hold_entity(),)), capsys)
    assert (code, out.strip()) == (3, "HELD")
    assert "source=node" in err
    assert "holds=hold-a" in err
    assert (tmp_path / "journal.jsonl").read_text().count("\n") == 1


def test_no_hold_exits_0_clear_from_the_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(tmp_path, FakeStateReader(()), capsys)
    assert (code, out.strip()) == (0, "CLEAR")
    assert "source=node" in err


def test_a_node_fault_with_no_file_lines_exits_2_undecided(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, _ = run(tmp_path, state, capsys)
    assert (code, out.strip()) == (2, "UNDECIDED")


def test_the_file_fallback_is_clear_and_logged_as_file_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    holds = tmp_path / "file-holds.txt"
    holds.write_text("HOLD none checked=0 released=0 expired=0 legacy-no-id=0\n")
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, err = run(tmp_path, state, capsys, "--file-holds", str(holds))
    assert (code, out.strip()) == (0, "CLEAR")
    assert "source=file-fallback" in err


def test_a_retired_fallback_is_undecided_even_with_file_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    holds = tmp_path / "file-holds.txt"
    holds.write_text("HOLD none checked=0 released=0 expired=0 legacy-no-id=0\n")
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, _ = run(
        tmp_path, state, capsys, "--file-holds", str(holds), fallback=False
    )
    assert (code, out.strip()) == (2, "UNDECIDED")
