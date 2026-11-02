# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity receipt counts, per read verb, how the file and the database answer it (OMN-21885 AC2).

``verb_diff=`` carries one ``verb:N`` per read verb: N is the number of answer lines the file
and the database do not share (each side's surplus, counted as often as it occurs). A verb whose
read failed, or whose database read fell back to the file, is ``verb:err``, never zero.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_work_ledger.parity import (
    compare,
    main,
    receipt_row,
)
from omnimarket.nodes.node_projection_work_ledger.parity_verbs import (
    READ_VERBS,
    VerbReadError,
    VerbRun,
    format_verb_diff,
    normalize_answer,
    ruling_tickets,
    run_verb_diff,
    subprocess_verb_run,
    verb_calls,
)

pytestmark = pytest.mark.unit

DAY = "2026-10-27"
HOLD = "2026-10-27T10:10:00Z | HOLD | lane=beta | id=2026-10-27T10:10:00Z-beta | surface=lab-dev | until=open | reserved"
RELEASE = "2026-10-27T11:00:00Z | RELEASE | lane=beta | re=2026-10-27T10:10:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done"
STATUS = "2026-10-27T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress"
RULING = '2026-10-27T09:00:00Z | RULING | lane=orch | ticket=OMN-7 | "go" | text'
FILE_ROWS = [HOLD, RELEASE, STATUS, RULING]
DB_ROWS = [HOLD, STATUS, RULING]


def _held(rows: list[str]) -> list[str]:
    released = {
        cell.removeprefix("re=")
        for row in rows
        if " | RELEASE | " in row
        for cell in row.split(" | ")
        if cell.startswith("re=")
    }
    ids = [
        cell.removeprefix("id=")
        for row in rows
        if " | HOLD | " in row
        for cell in row.split(" | ")
        if cell.startswith("id=")
    ]
    return [i for i in ids if i not in released]


def _stub_run(file_rows: list[str], db_rows: list[str]) -> VerbRun:
    """Answer each read verb from the rows its source holds, the way the verbs print them."""

    def run(args: tuple[str, ...], source: str) -> str:
        rows = file_rows if source == "file" else db_rows
        verb = args[0]
        if verb == "holds":
            held = _held(rows)
            return "\n".join(
                [f"HOLD held={len(held)} checked=1", *(f"HELD {h}" for h in held)]
            )
        if verb == "query":
            return "\n".join(
                [f"ROWS n={len(rows)}", *(f"L{i} {r}" for i, r in enumerate(rows, 1))]
            )
        return f"{verb} ok"

    return run


def _rid(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cells(row: str) -> dict[str, str]:
    cells = [c.strip() for c in row.split(" | ")]
    return dict(c.split("=", 1) for c in cells[2:] if "=" in c and " " not in c)


def _diff(file_rows: list[str], db_rows: list[str]) -> dict[str, int | None]:
    calls = verb_calls(DAY, ruling_tickets(file_rows))
    return run_verb_diff(_stub_run(file_rows, db_rows), calls)


def test_verb_diff_names_every_read_verb() -> None:
    assert READ_VERBS == (
        "query", "holds", "inbox", "catchup", "open-rulings", "open-claims",
    )  # fmt: skip

    diff = _diff(FILE_ROWS, FILE_ROWS)

    assert tuple(diff) == READ_VERBS
    assert set(diff.values()) == {0}


def test_verb_diff_a_hold_released_in_the_file_only_raises_holds() -> None:
    diff = _diff(FILE_ROWS, DB_ROWS)

    # the file holds nothing, the database still holds beta: the header and the item differ
    assert diff["holds"] == 3
    assert diff["query"] == 3
    assert diff["inbox"] == 0
    assert diff["open-claims"] == 0


def test_verb_diff_receipt_row_carries_the_counts() -> None:
    report = compare(
        file_rows=FILE_ROWS,
        projection_rows={_rid(r): r[:20] for r in FILE_ROWS},
        projection_state={},
        since=datetime(2026, 10, 27, tzinfo=UTC),
        until=datetime(2026, 10, 27, 23, 59, 59, tzinfo=UTC),
        projection_raw_rows={_rid(r): r for r in FILE_ROWS},
    )
    now = datetime(2026, 11, 2, tzinfo=UTC)

    raised = receipt_row(report, DAY, now, verb_diff=_diff(FILE_ROWS, DB_ROWS))
    agree = receipt_row(report, DAY, now, verb_diff=_diff(FILE_ROWS, FILE_ROWS))
    unread = receipt_row(report, DAY, now)

    assert _cells(raised)["verb_diff"].split(",")[1] == "holds:3"
    assert _cells(agree)["verb_diff"] == (
        "query:0,holds:0,inbox:0,catchup:0,open-rulings:0,open-claims:0"
    )
    assert _cells(unread)["verb_diff"] == "not-run"


def test_verb_diff_a_failed_read_is_err_never_zero() -> None:
    def run(args: tuple[str, ...], source: str) -> str:
        if source == "db" and args[0] == "holds":
            raise VerbReadError("the read command exited 2")
        return f"{args[0]} ok"

    diff = run_verb_diff(run, verb_calls(DAY, []))

    assert diff["holds"] is None
    assert diff["query"] == 0
    assert format_verb_diff(diff).split(",")[1] == "holds:err"


def test_verb_diff_open_rulings_runs_once_per_ruling_ticket() -> None:
    rows = [RULING, RULING.replace("OMN-7", "OMN-8"), STATUS]
    calls = verb_calls(DAY, ruling_tickets(rows))

    assert [c for c in calls if c[0] == "open-rulings"] == [
        ("open-rulings", ("open-rulings", "--ticket", "OMN-7")),
        ("open-rulings", ("open-rulings", "--ticket", "OMN-8")),
    ]


def test_verb_diff_normalize_masks_what_changes_between_reads() -> None:
    file_side = (
        "open-claims claims=3 open=1 ledger-newest=2026-10-27T11:00:00Z now=2026-11-02T00:00:00Z\n"
        "OPEN-CLAIM lane=a claim=2026-10-27T10:00:00Z ROLLING_WORK_LEDGER_x-split.md:L1894 age=1h2m\n"
        "catchup rows=3 detail=/tmp/a/catchup-1.txt\n"
    )
    db_side = (
        "open-claims claims=3 open=1 ledger-newest=2026-10-27T11:00:05Z now=2026-11-02T00:00:09Z\n"
        "OPEN-CLAIM lane=a claim=2026-10-27T10:00:00Z L77 age=1h2m\n"
        "catchup rows=3 detail=/tmp/b/catchup-2.txt\n"
    )

    assert normalize_answer(file_side) == normalize_answer(db_side)
    assert normalize_answer(file_side) != normalize_answer(
        db_side.replace("open=1", "open=2")
    )


FAKE_COMMAND = """\
import json, os, sys
args = sys.argv[1:]
source = args[args.index("--source") + 1]
verb = args[0]
state = json.load(open(os.environ["FAKE_VERB_STATE"]))
if source == "db" and state.get("db_down"):
    print("source=file-fallback verb=" + verb, file=sys.stderr)
    print(state["file"][verb])
    sys.exit(0)
print(state[source][verb])
"""


def _fake_command(tmp_path: Path, state: Mapping[str, object]) -> list[str]:
    script = tmp_path / "fake_onex_ledger.py"
    script.write_text(FAKE_COMMAND)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "verb_state.json").write_text(json.dumps(state))
    return [sys.executable, str(script)]


def test_verb_diff_subprocess_runner_counts_a_differing_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {v: f"{v} rows=3" for v in READ_VERBS}
    state = {"file": answers, "db": answers | {"holds": "HOLD held=1"}}
    command = _fake_command(tmp_path, state)
    monkeypatch.setenv("FAKE_VERB_STATE", str(tmp_path / "verb_state.json"))
    run = subprocess_verb_run(
        command, ledger=tmp_path / "L.md", detail_dir=tmp_path / "detail", timeout=30
    )

    diff = run_verb_diff(run, verb_calls(DAY, []))

    assert diff["holds"] == 2
    assert diff["query"] == 0


def test_verb_diff_a_database_read_that_fell_back_to_the_file_is_err(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {v: f"{v} rows=3" for v in READ_VERBS}
    command = _fake_command(tmp_path, {"file": answers, "db": answers, "db_down": True})
    monkeypatch.setenv("FAKE_VERB_STATE", str(tmp_path / "verb_state.json"))
    run = subprocess_verb_run(
        command, ledger=tmp_path / "L.md", detail_dir=tmp_path / "detail", timeout=30
    )

    diff = run_verb_diff(run, verb_calls(DAY, ["OMN-7"]))

    assert set(diff.values()) == {None}


def test_verb_diff_cli_receipt_without_a_verb_command_says_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(sorted(FILE_ROWS)) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": _rid(r), "row_ts": r[:20], "source": "x", "raw_row": r}
                    for r in FILE_ROWS
                ],
                "state": [],
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir()

    main(
        [
            "--ledger", str(ledger), "--utc-day", DAY, "--projection-json", str(export),
            "--state-dir", str(state), "--receipt",
        ]
    )  # fmt: skip

    [row] = capsys.readouterr().out.splitlines()
    assert _cells(row)["verb_diff"] == "not-run"


def test_verb_diff_cli_receipt_runs_the_verbs_on_both_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(sorted(FILE_ROWS)) + "\n")
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": _rid(r), "row_ts": r[:20], "source": "x", "raw_row": r}
                    for r in FILE_ROWS
                ],
                "state": [],
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir()
    answers = {v: f"{v} rows=3" for v in READ_VERBS}
    command = _fake_command(
        tmp_path, {"file": answers, "db": answers | {"holds": "HOLD held=1"}}
    )
    monkeypatch.setenv("FAKE_VERB_STATE", str(tmp_path / "verb_state.json"))

    main(
        [
            "--ledger", str(ledger), "--utc-day", DAY, "--projection-json", str(export),
            "--state-dir", str(state), "--receipt",
            "--verb-command", " ".join(command),
        ]
    )  # fmt: skip

    [row] = capsys.readouterr().out.splitlines()
    assert _cells(row)["verb_diff"] == (
        "query:0,holds:2,inbox:0,catchup:0,open-rulings:0,open-claims:0"
    )
