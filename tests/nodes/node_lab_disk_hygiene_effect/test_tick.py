# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the lab disk hygiene node (OMN-23707)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_hygiene_effect.handlers.handler_lab_disk_hygiene import (
    HandlerLabDiskHygiene,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.runtime_lab_disk_hygiene_tick import (
    main,
    run_once,
)

pytestmark = pytest.mark.unit

OLD_ROW = "a4\t2026-10-01T00:00:00.000000000Z\n"


class FakeDocker:
    """Return a partial or failed inspect and record every Docker call."""

    def __init__(self, inspect_stdout: str, inspect_exit: int = 1) -> None:
        self.inspect_stdout = inspect_stdout
        self.inspect_exit = inspect_exit
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        if argv[1] == "version":
            out = "29.8.1\n"
        elif argv[1] == "ps":
            out = "a4\told-orphan\texited\t\ng1\t\tdead\t\n"
        elif argv[1] == "inspect":
            return subprocess.CompletedProcess(
                argv,
                self.inspect_exit,
                self.inspect_stdout,
                "error: no such object: g1",
            )
        return subprocess.CompletedProcess(argv, 0, out, "")


def _overlay(tmp: Path, census: Path) -> Path:
    overlay = tmp / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"census_path": str(census)}), encoding="utf-8")
    return overlay


def _handler(docker: FakeDocker) -> HandlerLabDiskHygiene:
    return HandlerLabDiskHygiene(
        run=docker.run,
        disk_free=lambda _: 100,
        which=lambda _: "docker",
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    )


def test_tick_ghost_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    census = tmp_path / "census.yaml"
    census.write_text("lanes: {}\n", encoding="utf-8")
    monkeypatch.setenv(
        "OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", str(_overlay(tmp_path, census))
    )
    docker = FakeDocker(OLD_ROW)
    code, line = run_once(
        registry_root=tmp_path, dry_run=False, handler=_handler(docker)
    )
    assert code == 0
    assert line["ghost_containers"] >= 1
    assert line["errors"] == []
    assert not any(
        call[1] == "rm" for call in docker.calls if call[1:2] == ["rm"] and "g1" in call
    )


def test_tick_census_unreadable_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    census = tmp_path / "missing-census.yaml"
    monkeypatch.setenv(
        "OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", str(_overlay(tmp_path, census))
    )
    docker = FakeDocker(OLD_ROW)
    code, line = run_once(
        registry_root=tmp_path, dry_run=False, handler=_handler(docker)
    )
    assert code == 1
    assert line["removed_containers"] == 0
    assert any("census" in error for error in line["errors"])
    assert not any(call[1] == "rm" for call in docker.calls)


def test_tick_census_undeclared_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    docker = FakeDocker(OLD_ROW)
    code, line = run_once(
        registry_root=tmp_path, dry_run=False, handler=_handler(docker)
    )
    assert code == 1
    assert line["removed_containers"] == 0
    assert not any(call[1] == "rm" for call in docker.calls)


def test_tick_engine_failure_naming_no_candidate_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control of the ghost golden: no age read and the engine names only one of two candidates, so it is an error."""
    census = tmp_path / "census.yaml"
    census.write_text("lanes: {}\n", encoding="utf-8")
    monkeypatch.setenv(
        "OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", str(_overlay(tmp_path, census))
    )
    docker = FakeDocker("")
    code, line = run_once(
        registry_root=tmp_path, dry_run=False, handler=_handler(docker)
    )
    assert code == 1
    assert line["removed_containers"] == 0
    assert any("docker inspect" in error for error in line["errors"])
    assert not any(call[1] == "rm" for call in docker.calls)


def test_tick_probe_loads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    code = main(["--probe"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "probe_ok"


def test_tick_writes_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    census = tmp_path / "census.yaml"
    census.write_text("lanes: {}\n", encoding="utf-8")
    monkeypatch.setenv(
        "OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", str(_overlay(tmp_path, census))
    )
    docker = FakeDocker(OLD_ROW)
    state_dir = tmp_path / "state"
    code = main(
        ["--omni-home", str(tmp_path), "--state-dir", str(state_dir)],
        handler=_handler(docker),
    )
    record = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert code == 0
    assert record["exit_code"] == 0
    assert record["result"]["status"] == "ok"
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_tick_overlay_refused_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY", "")
    state_dir = tmp_path / "state"
    code = main(
        [
            "--omni-home",
            str(tmp_path),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "overlay_refused"
    assert (state_dir / "last-run.json").exists()
