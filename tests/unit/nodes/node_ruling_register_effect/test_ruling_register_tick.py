# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick form of the ruling register: the unit's run reads the register and records the reading (OMN-24944).

Golden: a probe reads nothing and says ``PROBE ok``; a run prints one MISSING line per row that is not TRUE, one
``RULING-REGISTER`` summary line, exits 0 (a row that is not TRUE is the reading, not a failed tick) and writes
``last-run.json`` with the verdict counts. Error: with no overlay a probe and a run exit 3 and a run writes no
evidence. Failing control: a run with every row TRUE records zero FALSE and zero UNREAD, so the counts are read from
the verdicts and are not constants.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_ruling_register_effect import (
    runtime_ruling_register_tick as tick,
)
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick import (
    main,
)
from tests.unit.nodes.node_ruling_register_effect.test_ruling_register_effect import (
    cli_env,
    cli_row,
)

pytestmark = pytest.mark.unit


def _record(state_dir: Path) -> dict[str, object]:
    return json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))


def test_the_probe_says_ok_and_runs_no_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row(
                "row-marker",
                f"open({str(marker)!r}, 'w').close()",
                {"kind": "exit_zero"},
            )
        ],
    )
    assert main(["--probe"]) == 0
    assert capsys.readouterr().out == "PROBE ok\n"
    assert not marker.exists()


def test_the_probe_and_the_run_exit_3_with_no_overlay_and_the_run_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main(["--probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    state_dir = tmp_path / "state"
    assert main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)]) == 3
    assert "ONEX_SKILL_OVERLAY_ROOTS" in capsys.readouterr().err
    assert not (state_dir / "last-run.json").exists()


def test_a_run_prints_the_missing_lines_and_records_the_verdict_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row("row-true", "pass", {"kind": "exit_zero"}),
            cli_row("row-false", "print('leftover')", {"kind": "stdout_empty"}),
            cli_row("row-dies", "import sys; sys.exit(9)", {"kind": "exit_zero"}),
        ],
    )
    state_dir = tmp_path / "state"
    assert main(["--omni-home", str(tmp_path), "--state-dir", str(state_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" | ")[0] for line in lines[:-1]] == [
        "MISSING ruling-not-running:row-false",
        "MISSING ruling-not-running:row-dies",
    ]
    assert lines[-1] == "RULING-REGISTER rows=3 TRUE=1 FALSE=1 UNREAD=1"
    record = _record(state_dir)
    assert record["passed"] is False
    assert record["rows"] == 3
    assert record["counts"] == {"TRUE": 1, "FALSE": 1, "UNREAD": 1}
    assert record["verdicts"] == {
        "row-true": "TRUE",
        "row-false": "FALSE",
        "row-dies": "UNREAD",
    }


def test_failing_control_a_run_with_every_row_true_records_no_false_and_no_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(
        tmp_path,
        monkeypatch,
        [cli_row("row-true", "pass", {"kind": "exit_zero"})],
    )
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert capsys.readouterr().out == "RULING-REGISTER rows=1 TRUE=1 FALSE=0 UNREAD=0\n"
    record = _record(state_dir)
    assert record["passed"] is True
    assert record["counts"] == {"TRUE": 1, "FALSE": 0, "UNREAD": 0}


def test_a_run_with_no_state_dir_writes_nothing_anywhere_not_even_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = tmp_path / "cwd"
    working.mkdir()
    # A regression that wrote last-run.json beside the process (a default of ".") would land here, in the cwd; the
    # glob over tmp_path sees it because the cwd is under it and the file is not under any state directory.
    monkeypatch.chdir(working)
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    assert main([]) == 0
    assert list(working.iterdir()) == []
    assert not list(tmp_path.glob("**/last-run.json"))


def _bad_overlay_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A register overlay that exists but fails validation (its rows key is missing)."""
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    (tmp_path / "node_ruling_register_compute" / "overlay.yaml").write_text(
        "max_reading_age_seconds: 600\n", encoding="utf-8"
    )


def test_an_invalid_overlay_exits_3_on_the_probe_and_on_the_run_and_the_run_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bad_overlay_env(tmp_path, monkeypatch)
    assert main(["--probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert not (state_dir / "last-run.json").exists()


LOADER_ERRORS = [
    NodeOverlayError("overlay missing"),
    ValueError("a loader let a bare ValueError through"),
    OSError("a loader let an OSError through"),
    ValidationError.from_exception_data("ModelRulingRoute", []),
]
LOADER_ERROR_IDS = ["node-overlay-error", "value-error", "os-error", "validation-error"]


@pytest.mark.parametrize("error", LOADER_ERRORS, ids=LOADER_ERROR_IDS)
@pytest.mark.parametrize("loader", ["load_ruling_register_policy", "load_route"])
def test_the_probe_and_the_run_exit_3_when_a_loader_raises_and_never_traceback(
    error: Exception,
    loader: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(tick, loader, refuse)
    assert main(["--probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert main(["--state-dir", str(tmp_path / "state")]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert not (tmp_path / "state" / "last-run.json").exists()


def test_an_overlay_changed_after_the_tick_loaded_it_is_not_reloaded_and_never_tracebacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The tick loads the policy and the route once, under its loader guard, and hands them to the read: a register
    overlay rewritten between that load and the read is never loaded a second time outside the guard, so the tick
    neither tracebacks nor reads another policy than the one it loaded."""
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    (overlay,) = tmp_path.glob("node_ruling_register_compute/**/*.yaml")
    real_load_route = tick.load_route

    def load_route_then_break_the_register(*args: object, **kwargs: object) -> object:
        route = real_load_route(*args, **kwargs)  # type: ignore[arg-type]
        overlay.write_text("rows: [not, a, register]\n", encoding="utf-8")
        return route

    monkeypatch.setattr(tick, "load_route", load_route_then_break_the_register)
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "RULING-REGISTER rows=1 " in captured.out
    assert _record(state_dir)["verdicts"] == {"row-true": "TRUE"}


@pytest.mark.parametrize(
    "bug",
    [ValueError("a bug in the read"), OSError("a bug in the read")],
    ids=["value-error", "os-error"],
)
def test_a_value_error_or_os_error_raised_by_the_read_is_a_bug_and_not_indeterminate(
    bug: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])

    def broken_read(*_args: object, **_kwargs: object) -> object:
        raise bug

    monkeypatch.setattr(tick, "read_register", broken_read)
    state_dir = tmp_path / "state"
    with pytest.raises(type(bug), match="a bug in the read"):
        main(["--state-dir", str(state_dir)])
    assert "INDETERMINATE" not in capsys.readouterr().err
    assert not (state_dir / "last-run.json").exists()


def test_a_state_dir_that_cannot_be_written_exits_3_and_does_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    blocker = tmp_path / "state"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    assert main(["--state-dir", str(blocker)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_a_journal_write_that_fails_is_recorded_in_last_run_json_as_well_as_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = cli_env(
        tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})]
    )
    # The journal is a directory: every append to it fails, so the row's event is lost, not its reading.
    journal = tmp_path / "journal-dir"
    journal.mkdir()
    monkeypatch.setenv("ONEX_RULING_REGISTER_JOURNAL", str(journal))
    assert env["ONEX_RULING_REGISTER_JOURNAL"] != str(journal)
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert "RULING-REGISTER-JOURNAL-ERROR row-true" in capsys.readouterr().err
    record = _record(state_dir)
    assert record["journal_errors"] == ["row-true: IsADirectoryError"]
    assert record["verdicts"] == {"row-true": "TRUE"}


def test_a_run_with_a_working_journal_records_no_journal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert _record(state_dir)["journal_errors"] == []


def test_a_missing_journal_parent_directory_is_created_and_the_journal_line_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unit's journal sits under ``<registry_root>/.onex_state/ruling-register``; the sink creates the parents."""
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    journal = tmp_path / "omni" / ".onex_state" / "ruling-register" / "journal.jsonl"
    assert not journal.parent.exists()
    monkeypatch.setenv("ONEX_RULING_REGISTER_JOURNAL", str(journal))
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert capsys.readouterr().err == ""
    (line,) = journal.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["row_id"] == "row-true"
    record = _record(state_dir)
    assert record["journal_errors"] == []
    assert record["journal"] == str(journal)


def test_a_journal_parent_that_cannot_be_created_fails_loud_with_a_line_and_a_record_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    blocker = tmp_path / "omni"
    blocker.write_text("a file where the registry root should be", encoding="utf-8")
    journal = blocker / ".onex_state" / "ruling-register" / "journal.jsonl"
    monkeypatch.setenv("ONEX_RULING_REGISTER_JOURNAL", str(journal))
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert (
        "RULING-REGISTER-JOURNAL-ERROR row-true: NotADirectoryError"
        in capsys.readouterr().err
    )
    record = _record(state_dir)
    assert record["journal_errors"] == ["row-true: NotADirectoryError"]
    assert record["verdicts"] == {"row-true": "TRUE"}


def test_a_route_whose_journal_variable_the_environment_does_not_set_records_no_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The events stay in memory and the record says so, so a check can tell a journal that is not written."""
    cli_env(tmp_path, monkeypatch, [cli_row("row-true", "pass", {"kind": "exit_zero"})])
    monkeypatch.delenv("ONEX_RULING_REGISTER_JOURNAL", raising=False)
    state_dir = tmp_path / "state"
    assert main(["--state-dir", str(state_dir)]) == 0
    assert _record(state_dir)["journal"] == ""
