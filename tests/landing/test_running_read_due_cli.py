# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26433: the running-read due node has a call line, ``python -m omnimarket.nodes.node_running_read_due_compute``.

The node declares a direct-call trigger and no topic. Before this change nothing could call it from a schedule line: the
landing tick runs observe only and its snapshot records carry no merged pull requests or ledger text. The call line reads
the ledger file and prints, as one JSON line, the open due rows, the alarm rows owed and the tickets held out of Done;
``write-due`` prints the due row one merge maps to. It appends nothing: the rows it prints are the caller's to append.

The falsifier: at the merge base the module has no entry point, so every test here fails to import it.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar

pytestmark = pytest.mark.unit

DUE_ID = "rrd:OmniNode-ai/omnimarket:6700"
MERGED = {
    "pr": "OmniNode-ai/omnimarket#6700",
    "ticket": "OMN-26491",
    "owner": "orchestrator-31d9-swarm",
    "merged_at": "2026-11-23T00:30:00Z",
    "runtime_affecting": True,
    "pr_body": "Running read: node=node_floor_fill_tick topic=onex.evt.floor.fill.v1",
}


def _main():  # imported inside the test so a missing module fails the test, not the collection
    from omnimarket.nodes.node_running_read_due_compute import runtime_running_read_due

    return runtime_running_read_due.main


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _write_due(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    _stdin(monkeypatch, json.dumps(MERGED))
    code = _main()(["write-due"])
    assert code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_write_due_prints_the_due_row_of_a_runtime_affecting_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _write_due(monkeypatch, capsys)
    assert out["due_id"] == DUE_ID
    row = grammar.parse_row(str(out["rows"]))
    assert row is not None
    assert row.row_type == "STATUS"
    assert row.value("kind") == "running-read-due"


def test_read_due_prints_the_alarm_owed_for_a_due_row_with_no_run_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    due = _write_due(monkeypatch, capsys)
    _stdin(monkeypatch, str(due["rows"]) + "\n")
    code = _main()(
        ["read-due", "--now", datetime(2026, 11, 23, 1, 31, tzinfo=UTC).isoformat()]
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert out["open_due_ids"] == [DUE_ID]
    assert out["blocked_tickets"] == ["OMN-26491"]
    assert [a["due_id"] for a in out["alarms"]] == [DUE_ID]
    alarm = grammar.parse_row(out["alarms"][0]["rows"])
    assert alarm is not None
    assert alarm.value("kind") == "running-read-alarm"


def test_read_due_raises_no_alarm_when_a_run_line_closes_the_due_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    due = _write_due(monkeypatch, capsys)
    observed = (
        "2026-11-23T01:10:00Z | STATUS | lane=orchestrator-31d9-swarm | ticket=OMN-26491 | "
        f"due_id={DUE_ID} | observed=node_floor_fill_tick consumed onex.evt.floor.fill.v1 | "
        "read_at=2026-11-23T01:10:00Z | the change is running"
    )
    _stdin(monkeypatch, str(due["rows"]) + "\n" + observed + "\n")
    code = _main()(["read-due", "--now", "2026-11-23T02:00:00+00:00"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert out["alarms"] == []
    assert out["blocked_tickets"] == []


def test_read_due_of_an_empty_ledger_is_refused_never_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "")
    code = _main()(["read-due"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3
    assert out["refusal"]
    assert out["alarms"] == []
