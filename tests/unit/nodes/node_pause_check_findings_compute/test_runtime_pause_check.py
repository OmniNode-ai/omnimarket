# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pause check command end to end on the in-memory path (OMN-24233, cutover plan step L6).

A hold only in the live log pauses (``source=node``); the live answer wins over the file in both
directions; a node fault falls back to the file's HOLD findings, counted, while the hold verdict
overlay allows it, and is UNDECIDED (never CLEAR) once it does not; a batch reads the live log once.
Each decision emits one event: golden ``answered`` or error ``refused``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_pause_check_runtime_effect.runtime_pause_check import main
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

NOW = "2026-11-13T12:00:00Z"
HOLD_ROW = (
    "2026-11-10T10:00:00Z | HOLD | lane=orch-1 | id=hold-a | pr=omnimarket#7 | "
    "to=lane-x | until=2026-11-30T00:00Z | keep omnimarket#7 out"
)
OTHER_ROW = "2026-11-10T10:01:00Z | STATUS | lane=orch-2 | pr=omnimarket#99 | ok"
READER_OVERLAY = """\
moved_readers: [pause-check]
max_watermark_age_seconds: 99999999
page_size: 200
max_pages: 50
route: {kind: inmemory}
parity: {receipt_lane: work-ledger-parity, window_hours: 48}
events: {journal_env: ONEX_LEDGER_READER_JOURNAL}
"""
RULES = {
    "report_types": ["STATUS", "TERMINAL", "RELEASE", "FRICTION", "CORRECTION", "ACK"],
    "landing_order": "landing-order",
    "parked": {},
    "repo_pause_lines": {},
}


def env(
    tmp_path: Path, *, file_rows: tuple[str, ...], fallback: bool
) -> dict[str, str]:
    (tmp_path / "reader.yaml").write_text(READER_OVERLAY)
    (tmp_path / "verdict.yaml").write_text(
        f"file_fallback_allowed: {str(fallback).lower()}\n"
        "journal_env: ONEX_LEDGER_HOLD_VERDICT_JOURNAL\n"
    )
    (tmp_path / "pause.yaml").write_text(
        "snippet_bytes: 220\nmax_order_tokens: 8\n"
        "ledger_env: ONEX_PAUSE_CHECK_LEDGER_FILE\njournal_env: ONEX_PAUSE_CHECK_JOURNAL\n"
    )
    (tmp_path / "ledger.md").write_text("\n".join(file_rows) + "\n")
    (tmp_path / "rules.json").write_text(json.dumps(RULES))
    return {
        "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
        "ONEX_LEDGER_HOLD_VERDICT_OVERLAY": str(tmp_path / "verdict.yaml"),
        "ONEX_LEDGER_HOLD_VERDICT_JOURNAL": str(tmp_path / "hold-journal.jsonl"),
        "ONEX_PAUSE_CHECK_OVERLAY": str(tmp_path / "pause.yaml"),
        "ONEX_PAUSE_CHECK_JOURNAL": str(tmp_path / "journal.jsonl"),
        "ONEX_PAUSE_CHECK_LEDGER_FILE": str(tmp_path / "ledger.md"),
    }


def run(
    tmp_path: Path,
    state: FakeStateReader,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...] = ("--pr", "omnimarket#7", "--lane", "lane-x"),
    *,
    file_rows: tuple[str, ...] = (OTHER_ROW,),
    fallback: bool = True,
    environ: dict[str, str] | None = None,
) -> tuple[int, str, str, QueryNodePointRead]:
    reader = QueryNodePointRead(state)
    code = main(
        [*argv, "--rules-json", str(tmp_path / "rules.json"), "--now", NOW],
        environ=environ or env(tmp_path, file_rows=file_rows, fallback=fallback),
        point_read=reader,
        clock=lambda: datetime(2026, 11, 13, 12, tzinfo=UTC),
    )
    out = capsys.readouterr()
    return code, out.out, out.err, reader


def test_a_hold_only_in_the_live_log_pauses_with_source_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _, _ = run(tmp_path, FakeStateReader((pr_hold_entity(),)), capsys)
    assert code == 3
    assert "HOLDS source=node fallbacks=0 reads=1" in out
    assert "HOLD hold-a:" in out
    assert "VERDICT: PAUSED" in out


def test_the_live_answer_wins_over_the_file_when_the_projection_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: the file holds the row, the live log does not, and the answer is CLEAR from the node."""
    code, out, _, _ = run(tmp_path, FakeStateReader(()), capsys, file_rows=(HOLD_ROW,))
    assert code == 0
    assert "HOLDS source=node fallbacks=0 reads=1" in out
    assert "VERDICT: CLEAR" in out
    assert "HOLD hold-a" not in out


def test_a_node_fault_falls_back_to_the_file_and_counts_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, err, _ = run(tmp_path, state, capsys, file_rows=(HOLD_ROW,))
    assert code == 3
    assert "HOLDS source=file-fallback fallbacks=1 reads=1" in out
    assert "HOLD 1:" in out
    assert "PAUSE-CHECK pr=omnimarket#7" in err
    assert "source=file-fallback" in err


def test_a_node_fault_with_the_fallback_retired_is_undecided_never_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, err, _ = run(tmp_path, state, capsys, fallback=False)
    assert code == 2
    assert "VERDICT: CLEAR" not in out
    assert "a stop, never CLEAR" in err
    # the control: with the fallback allowed the same fault and the same file read CLEAR, so the
    # retired flag is what makes the fault a stop
    code, out, _, _ = run(tmp_path, state, capsys, fallback=True)
    assert (code, "source=file-fallback" in out, "VERDICT: CLEAR" in out) == (
        0,
        True,
        True,
    )


def test_the_batch_reads_the_live_log_once_and_lists_every_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prs = tmp_path / "prs.json"
    prs.write_text(
        json.dumps(
            [
                {"pr": "omnimarket#7", "labels": [], "lane": "lane-x"},
                {"pr": "omnimarket#8", "labels": []},
                {"pr": "repo-a#9", "labels": []},
            ]
        )
    )
    state = FakeStateReader((pr_hold_entity(),))
    code, out, _, reader = run(
        tmp_path, state, capsys, ("--prs-json", str(prs), "--format", "json")
    )
    assert len(reader.requests) == 1
    assert code == 0
    doc = json.loads(out)
    assert (doc["reads"], doc["fallbacks"], doc["undecided"]) == (1, 0, [])
    assert [(r["pr"], r["verdict"], r["source"]) for r in doc["results"]] == [
        ("omnimarket#7", "PAUSED", "node"),
        ("omnimarket#8", "CLEAR", "node"),
        ("repo-a#9", "CLEAR", "node"),
    ]
    # the control: one call per pull request reads the live log three times
    reads = 0
    for pr in ("omnimarket#7", "omnimarket#8", "repo-a#9"):
        _, _, _, per_pr = run(tmp_path, state, capsys, ("--pr", pr))
        reads += len(per_pr.requests)
    assert reads == 3


def test_golden_and_error_events_reach_the_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, FakeStateReader((pr_hold_entity(),)), capsys)
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    run(tmp_path, state, capsys, fallback=False)
    events = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl").read_text().splitlines()
    ]
    assert [(e["topic"], e["verdict"]) for e in events] == [
        ("onex.evt.omnimarket.pause-check-answered.v1", "PAUSED"),
        ("onex.evt.omnimarket.pause-check-refused.v1", "UNDECIDED"),
    ]


def test_an_unreadable_ledger_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = env(tmp_path, file_rows=(OTHER_ROW,), fallback=True)
    environment["ONEX_PAUSE_CHECK_LEDGER_FILE"] = str(tmp_path / "missing.md")
    code, out, err, _ = run(tmp_path, FakeStateReader(()), capsys, environ=environment)
    assert code == 2
    assert out == ""
    assert "ledger not readable" in err


def test_labels_still_pause_through_the_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ("--pr", "omnimarket#8", "--labels", "hold")
    code, out, _, _ = run(tmp_path, FakeStateReader(()), capsys, argv)
    assert code == 3
    assert "LABEL hold" in out
