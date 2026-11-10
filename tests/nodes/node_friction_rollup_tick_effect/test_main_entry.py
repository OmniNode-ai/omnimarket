# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The launchd entry of the friction rollup tick: arguments, environment, exit codes and last-run.json (OMN-22987)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_friction_rollup_tick_effect.runtime_friction_rollup_tick import (
    LEDGER_VARIABLE,
    main,
)
from tests.nodes.node_friction_rollup_tick_effect.tick_support import FIXTURES

pytestmark = pytest.mark.unit

NOW = "2026-09-26T12:00:00Z"


def _world(tmp_path: Path, appender_exit: int) -> tuple[Path, Path, Path, Path]:
    """A registry root, a ledger copy of the recorded rows, an evidence dir and a recording appender stub."""
    root = tmp_path / "registry"
    root.mkdir()
    ledger = tmp_path / "ledger" / "ledger_copy.md"
    ledger.parent.mkdir()
    ledger.write_text(
        (FIXTURES / "ledger_real_rows.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    evidence = tmp_path / "evidence"
    recorded = tmp_path / "appended.txt"
    stub = tmp_path / "stub_ledger.py"
    stub.write_text(
        "import sys\n"
        f"open({str(recorded)!r}, 'a').write(sys.argv[3] + '\\n')\n"
        f"sys.exit({appender_exit})\n",
        encoding="utf-8",
    )
    return root, ledger, evidence, stub


def _run(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    ledger: Path,
    evidence: Path,
    stub: Path,
    *extra: str,
) -> int:
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    monkeypatch.setenv("ONEX_LEDGER_COMMAND", f"{sys.executable} {stub}")
    return main(
        ["--omni-home", str(root), "--state-dir", str(evidence), "--now", NOW, *extra]
    )


def test_a_tick_writes_reports_under_the_registry_root_and_last_run_into_the_evidence_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, ledger, evidence, stub = _world(tmp_path, 0)
    assert _run(monkeypatch, root, ledger, evidence, stub) == 0
    assert (root / ".onex_state/friction-rollup/latest.json").is_file()
    last = json.loads((evidence / "last-run.json").read_text(encoding="utf-8"))
    assert last["state"] == "OK"
    assert last["exit_code"] == 0
    assert last["written_at"] == NOW
    assert "friction-rollup " in capsys.readouterr().out
    appended = (tmp_path / "appended.txt").read_text(encoding="utf-8").splitlines()
    assert len(appended) == 1
    assert "lane=friction-rollup-timer" in appended[0]


def test_a_dry_run_prints_the_row_and_writes_neither_reports_nor_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, ledger, evidence, stub = _world(tmp_path, 0)
    assert _run(monkeypatch, root, ledger, evidence, stub, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "| STATUS |" in out
    assert "state=DRY-RUN" in out
    assert not evidence.exists()
    assert not (root / ".onex_state").exists()
    assert not (tmp_path / "appended.txt").exists()


def test_a_refused_append_exits_1_and_leaves_no_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ledger, evidence, stub = _world(tmp_path, 1)
    assert _run(monkeypatch, root, ledger, evidence, stub) == 1
    assert not (evidence / "last-run.json").exists()


def test_an_absent_ledger_exits_2_and_leaves_no_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ledger, evidence, stub = _world(tmp_path, 0)
    ledger.unlink()
    assert _run(monkeypatch, root, ledger, evidence, stub) == 2
    assert not (evidence / "last-run.json").exists()


def test_an_unset_ledger_variable_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_VARIABLE, raising=False)
    with pytest.raises(SystemExit) as raised:
        main(["--omni-home", str(tmp_path)])
    assert raised.value.code == 2
