# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The launchd entry of the friction rollup tick: arguments, environment, exit codes and last-run.json (OMN-22987).

The tick reads its window and appends its row over the bus (OMN-26329): these tests run the entry against a bus read command
and a bus append command that are separate processes, the routes the installed unit's environment names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_friction_rollup_tick_effect.runtime_friction_rollup_tick import (
    main,
)
from tests.nodes.node_friction_rollup_tick_effect.tick_support import (
    FIXTURES,
    appended_rows,
    ledger_files,
    write_bus_stubs,
)

pytestmark = pytest.mark.unit

NOW = "2026-09-26T12:00:00Z"


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, append_exit: int
) -> tuple[Path, Path, Path]:
    """A registry root, an evidence dir and the append log of the bus commands, with the unit's bus environment set."""
    root = tmp_path / "registry"
    root.mkdir()
    ledger = FIXTURES / "ledger_real_rows.md"
    read_command, append_command = write_bus_stubs(tmp_path / "bin")
    log = tmp_path / "appended.jsonl"
    monkeypatch.setenv("ONEX_LEDGER_BUS_READ_COMMAND", read_command)
    monkeypatch.setenv("ONEX_LEDGER_BUS_APPEND_COMMAND", append_command)
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h-test")
    monkeypatch.setenv(
        "STUB_LEDGER_FILES", os.pathsep.join(str(f) for f in ledger_files(ledger))
    )
    monkeypatch.setenv("STUB_APPEND_LOG", str(log))
    monkeypatch.setenv("STUB_APPEND_EXIT", str(append_exit))
    return root, tmp_path / "evidence", log


def _run(root: Path, evidence: Path, *extra: str) -> int:
    return main(
        ["--omni-home", str(root), "--state-dir", str(evidence), "--now", NOW, *extra]
    )


def test_a_tick_writes_reports_under_the_registry_root_and_last_run_into_the_evidence_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence, log = _world(tmp_path, monkeypatch, 0)
    assert _run(root, evidence) == 0
    assert (root / ".onex_state/friction-rollup/latest.json").is_file()
    last = json.loads((evidence / "last-run.json").read_text(encoding="utf-8"))
    assert last["state"] == "OK"
    assert last["exit_code"] == 0
    assert last["written_at"] == NOW
    assert "friction-rollup " in capsys.readouterr().out
    appended = appended_rows(log)
    assert len(appended) == 1
    assert "lane=friction-rollup-timer" in appended[0]
    # The report was drawn from the bus read: the recorded FRICTION rows are in it.
    report = json.loads(
        (root / ".onex_state/friction-rollup/latest.json").read_text(encoding="utf-8")
    )
    assert report["windows"]["24h"]["totals"]["friction_rows"] > 0
    assert [s["path"] for s in report["windows"]["24h"]["sources"]] == [
        "ledger-bus-read"
    ]


def test_a_dry_run_prints_the_row_and_writes_neither_reports_nor_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence, log = _world(tmp_path, monkeypatch, 0)
    assert _run(root, evidence, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "| STATUS |" in out
    assert "state=DRY-RUN" in out
    assert not evidence.exists()
    assert not (root / ".onex_state").exists()
    assert not log.exists()


def test_a_refused_append_exits_1_and_leaves_no_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, evidence, _ = _world(tmp_path, monkeypatch, 65)
    assert _run(root, evidence) == 1
    assert not (evidence / "last-run.json").exists()


def test_an_unanswered_read_exits_2_and_leaves_no_last_run_and_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, evidence, log = _world(tmp_path, monkeypatch, 0)
    monkeypatch.setenv("STUB_READ_FAIL", "3")
    assert _run(root, evidence) == 2
    assert not (evidence / "last-run.json").exists()
    assert not log.exists()


def test_an_unset_read_command_exits_2_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence, log = _world(tmp_path, monkeypatch, 0)
    monkeypatch.delenv("ONEX_LEDGER_BUS_READ_COMMAND")
    assert _run(root, evidence) == 2
    assert "ONEX_LEDGER_BUS_READ_COMMAND is not set" in capsys.readouterr().err
    assert not log.exists()


def test_the_entry_takes_no_ledger_file_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, evidence, _ = _world(tmp_path, monkeypatch, 0)
    for flag in ("--ledger", "--archive-dir"):
        with pytest.raises(SystemExit) as raised:
            _run(root, evidence, flag, str(tmp_path))
        assert raised.value.code == 2
