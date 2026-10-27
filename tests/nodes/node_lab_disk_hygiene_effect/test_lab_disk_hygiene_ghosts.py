# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ghost containers and unreadable ages preserve stopped candidates safely."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_disk_hygiene_effect.handlers.handler_lab_disk_hygiene import (
    HandlerLabDiskHygiene,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models.model_lab_disk_hygiene_request import (
    ModelLabDiskHygieneRequest,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models.model_lab_disk_hygiene_result import (
    ModelLabDiskHygieneResult,
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


def _handle(tmp: Path, docker: FakeDocker) -> ModelLabDiskHygieneResult:
    census = tmp / "census.yaml"
    census.write_text("lanes: {}\n")
    handler = HandlerLabDiskHygiene(
        run=docker.run,
        disk_free=lambda _: 100,
        which=lambda _: "docker",
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    )
    return handler.handle(ModelLabDiskHygieneRequest(tmp, census=census))


def test_ghost_is_kept_without_error_or_removal(tmp_path: Path) -> None:
    docker = FakeDocker(OLD_ROW)
    result = _handle(tmp_path, docker)
    assert result.errors == ()
    assert result.ghost_containers == ("g1",)
    assert "g1" in result.kept_containers
    assert result.removed_containers == ("old-orphan",)
    assert [call for call in docker.calls if call[1] == "rm"] == [
        ["docker", "rm", "a4"]
    ]


@pytest.mark.parametrize("stdout", ["", "\n \n"])
def test_inspect_failure_without_rows_keeps_every_candidate(
    tmp_path: Path, stdout: str
) -> None:
    docker = FakeDocker(stdout)
    result = _handle(tmp_path, docker)
    assert not any(call[1] == "rm" for call in docker.calls)
    assert result.kept_containers == ("old-orphan", "g1")
    assert result.removed_containers == ()
    assert result.ghost_containers == ()
    assert len(result.errors) == 1
    assert result.errors[0].startswith("docker inspect exit 1")


def test_only_ghost_candidates_when_the_engine_names_each_as_unknown(
    tmp_path: Path,
) -> None:
    """The engine prints nothing on stdout and exits 1 when every id is unknown."""
    ghost_a = "4a4825418e3e" + "0" * 52
    ghost_b = "2fe876bf5180" + "0" * 52

    def run(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[1] == "version":
            return subprocess.CompletedProcess(argv, 0, "29.2.1\n", "")
        if argv[1] == "ps":
            out = f"{ghost_a}\t\tdead\t\n{ghost_b}\t\tdead\t\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[1] == "inspect":
            err = (
                f"error: no such object: {ghost_a}\nerror: no such object: {ghost_b}\n"
            )
            return subprocess.CompletedProcess(argv, 1, "", err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    census = tmp_path / "census.yaml"
    census.write_text("lanes: {}\n")
    handler = HandlerLabDiskHygiene(
        run=run,
        disk_free=lambda _: 100,
        which=lambda _: "docker",
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    )
    result = handler.handle(ModelLabDiskHygieneRequest(tmp_path, census=census))
    assert result.errors == ()
    assert result.ghost_containers == (ghost_a[:12], ghost_b[:12])
    assert result.removed_containers == ()


def test_line_reports_ghost_count(tmp_path: Path) -> None:
    result = _handle(tmp_path, FakeDocker(OLD_ROW))
    assert "ghost_containers=1" in result.line()


def test_unparseable_finished_at_keeps_existing_removal_behavior(
    tmp_path: Path,
) -> None:
    docker = FakeDocker("a4\tgarbage\ng1\t0001-01-01T00:00:00Z\n", inspect_exit=0)
    result = _handle(tmp_path, docker)
    assert result.errors == ()
    assert result.ghost_containers == ()
    assert [call for call in docker.calls if call[1] == "rm"] == [
        ["docker", "rm", "a4", "g1"]
    ]
