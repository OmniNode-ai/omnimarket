# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""parity --explain says where each row missing from the projection was lost (OMN-20535 AC3).

Four classes: ``failure-log`` (the writer's dual write recorded the skip),
``journal-dead-letter`` (the hook-emit drainer evicted, dropped or dead-lettered the
record, per its loss log or its quarantine directory), ``journal-pending`` (still
queued in the journal) and ``unexplained`` (no evidence names it).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityLossClass,
)
from omnimarket.nodes.node_projection_work_ledger.parity import (
    explain_missing,
    load_explain_evidence,
    main,
    split_rows,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_producer_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI honours the producers' path overrides; a shell value must not leak in."""
    monkeypatch.delenv("ONEX_HOOK_EMIT_JOURNAL_DIR", raising=False)
    monkeypatch.delenv("ONEX_HOOK_EMIT_LOSS_LOG", raising=False)


ROWS = [
    "2026-10-03T01:00:00Z | STATUS | lane=a | one",
    "2026-10-03T02:00:00Z | STATUS | lane=b | two",
    "2026-10-03T03:00:00Z | STATUS | lane=c | three",
    "2026-10-03T04:00:00Z | STATUS | lane=d | four",
    "2026-10-03T05:00:00Z | STATUS | lane=e | five, projected",
]


def _rid(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _journal_record(directory: Path, name: str, raw: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "event_id": name,
                "event_type": "work.ledger.status",
                "payload": {"row_id": _rid(raw), "raw_row": raw},
                "correlation_id": _rid(raw),
                "queued_at": "2026-10-03T00:00:00+00:00",
            }
        )
    )
    return path


def _state(tmp_path: Path) -> Path:
    """A state directory laid out as the Mac's: failure log, journal, quarantine, loss log."""
    state = tmp_path / "state"
    journal = state / "hook_emit_journal"
    state.mkdir()
    (state / "work-ledger-emit-failures.jsonl").write_text(
        json.dumps(
            {"ts": "2026-10-03T01:00:01Z", "reason": "x", "row_id": _rid(ROWS[0])}
        )
        + "\n"
        + json.dumps({"reason": "an old line with no row id", "row_id": None})
        + "\n"
        + "not json\n"
    )
    _journal_record(journal, "00000000000000000002_b.json", ROWS[1])
    _journal_record(journal / "quarantine", "00000000000000000003_c.json", ROWS[2])
    (journal / "quarantine" / "00000000000000000003_c.reason.json").write_text("{}")
    return state


def test_explain_classifies_each_missing_row(tmp_path: Path) -> None:
    evidence = load_explain_evidence(state_dir=_state(tmp_path))
    result = explain_missing([_rid(r) for r in ROWS[:4]], evidence)

    by_row = {r.row_id: r.loss_class for r in result.rows}
    assert by_row == {
        _rid(ROWS[0]): EnumParityLossClass.FAILURE_LOG,
        _rid(ROWS[1]): EnumParityLossClass.JOURNAL_PENDING,
        _rid(ROWS[2]): EnumParityLossClass.JOURNAL_DEAD_LETTER,
        _rid(ROWS[3]): EnumParityLossClass.UNEXPLAINED,
    }
    assert (result.failure_log, result.journal_pending) == (1, 1)
    assert (result.journal_dead_letter, result.unexplained) == (1, 1)


def test_explain_a_journal_record_the_drainer_deleted_is_never_unexplained(
    tmp_path: Path,
) -> None:
    """The record is gone from the journal before the drainer published it; the drainer's
    loss line (OMN-20535 AC2, omniclaude hook_emit_journal.record_loss) names its row."""
    state = _state(tmp_path)
    journal = state / "hook_emit_journal"
    doomed = _journal_record(journal, "00000000000000000004_d.json", ROWS[3])
    doomed.unlink()
    (state / "hook_emit_journal_losses.jsonl").write_text(
        json.dumps(
            {
                "disposition": "dropped-over-bound",
                "journal_file": doomed.name,
                "row_id": _rid(ROWS[3]),
            }
        )
        + "\n"
    )

    result = explain_missing([_rid(ROWS[3])], load_explain_evidence(state_dir=state))

    assert [r.loss_class for r in result.rows] == [
        EnumParityLossClass.JOURNAL_DEAD_LETTER
    ]
    assert result.unexplained == 0
    assert "dropped-over-bound" in result.rows[0].detail


def test_explain_a_pending_record_outranks_an_older_failure_line(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    _journal_record(state / "hook_emit_journal", "00000000000000000009_a.json", ROWS[0])
    result = explain_missing([_rid(ROWS[0])], load_explain_evidence(state_dir=state))
    assert result.rows[0].loss_class is EnumParityLossClass.JOURNAL_PENDING


def test_explain_names_a_missing_evidence_source_rather_than_reading_it_as_empty(
    tmp_path: Path,
) -> None:
    evidence = load_explain_evidence(state_dir=tmp_path / "nowhere")
    result = explain_missing(["f" * 64], evidence)
    assert result.unexplained == 1
    assert set(result.evidence) == {"failure_log", "journal", "quarantine", "loss_log"}
    assert all(v.startswith("absent:") for v in result.evidence.values())


def test_explain_cli_adds_the_explain_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(ROWS) + "\n")
    assert len(split_rows(ledger.read_text())) == 5
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [{"row_id": _rid(ROWS[4]), "row_ts": "2026-10-03T05:00:00Z"}],
                "state": [],
            }
        )
    )
    args = [
        "--ledger", str(ledger), "--since", "2026-10-03T00:00:00Z",
        "--until", "2026-10-04T00:00:00Z", "--projection-json", str(export),
    ]  # fmt: skip

    assert main([*args, "--explain", "--state-dir", str(_state(tmp_path))]) == 1
    explain = json.loads(capsys.readouterr().out)["explain"]
    assert (explain["failure_log"], explain["journal_pending"]) == (1, 1)
    assert (explain["journal_dead_letter"], explain["unexplained"]) == (1, 1)

    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["explain"] is None


def test_explain_cli_without_a_state_dir_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(ROWS[0] + "\n")
    export = tmp_path / "projection.json"
    export.write_text(json.dumps({"rows": [], "state": []}))
    rc = main(
        [
            "--ledger", str(ledger), "--since", "2026-10-03T00:00:00Z",
            "--until", "2026-10-04T00:00:00Z", "--projection-json", str(export),
            "--explain",
        ]
    )  # fmt: skip
    assert rc == 2
    assert "state directory" in capsys.readouterr().err


def test_explain_reads_the_producers_journal_and_loss_log_overrides(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    journal = tmp_path / "elsewhere" / "journal"
    _journal_record(journal, "00000000000000000004_d.json", ROWS[3])
    loss_log = tmp_path / "losses.jsonl"
    loss_log.write_text(
        json.dumps({"row_id": _rid(ROWS[1]), "disposition": "dead-lettered"})
    )

    result = explain_missing(
        [_rid(ROWS[1]), _rid(ROWS[3])],
        load_explain_evidence(
            state_dir=state, journal_dir=journal, loss_log_path=loss_log
        ),
    )

    assert [r.loss_class for r in result.rows] == [
        EnumParityLossClass.JOURNAL_DEAD_LETTER,
        EnumParityLossClass.JOURNAL_PENDING,
    ]
    assert result.evidence["loss_log"] == str(loss_log)


def test_explain_reports_unreadable_journal_records(tmp_path: Path) -> None:
    state = _state(tmp_path)
    (state / "hook_emit_journal" / "00000000000000000007_x.json").write_text("{torn")

    result = explain_missing([], load_explain_evidence(state_dir=state))

    assert result.evidence["journal"].endswith("(unreadable=1)")
