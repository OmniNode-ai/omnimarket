# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A canonical clone whose core.worktree names a missing directory is repaired by the reconcile node."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_worktree_reconcile_effect.handlers import (
    adapter_core_worktree,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_core_worktree import (
    CommandStatusRowWriter,
    GitConfigFileCoreWorktree,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_core_worktree_repair import (
    HandlerCoreWorktreeRepair,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_core_worktree_repair import (
    EnumCoreWorktreeRepairKind,
    ModelCoreWorktreeRepairRequest,
    ModelCoreWorktreeRepairResult,
)
from omnimarket.nodes.node_worktree_reconcile_effect.runtime_worktree_reconcile import (
    main,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class Rows:
    """The ledger append boundary: one row per call."""

    def __init__(self) -> None:
        self.rows: list[str] = []

    def write(self, row: str) -> None:
        self.rows.append(row)


def run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=scrub_git_location_env(os.environ),
    )


def make_clone(registry: Path, name: str, worktree: str | None) -> Path:
    clone = registry / name
    clone.mkdir(parents=True)
    assert run_git(clone, "init", "--initial-branch=main").returncode == 0
    if worktree is not None:
        # Written to the file, as the stray run wrote it: git never resolves the value.
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(clone / ".git" / "config"),
                "core.worktree",
                worktree,
            ],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
    return clone


def stored(clone: Path) -> str:
    done = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(clone / ".git" / "config"),
            "--get",
            "core.worktree",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def handler(rows: Rows | None = None) -> HandlerCoreWorktreeRepair:
    return HandlerCoreWorktreeRepair(
        config=GitConfigFileCoreWorktree(), writer=rows, clock=FixedClock()
    )


def request(registry: Path, *, execute: bool = True) -> ModelCoreWorktreeRepairRequest:
    return ModelCoreWorktreeRepairRequest(
        host="h053",
        registries=(str(registry),),
        execute=execute,
        lane="worktree-reconcile",
        correlation_id=uuid4(),
    )


def test_core_worktree_missing_directory_is_unset_and_git_works_again(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    missing = tmp_path / "gone" / "fix1977-0815" / "clone"
    clone = make_clone(registry, "canonical", str(missing))
    # The fault reproduces: git cannot resolve the missing worktree.
    assert run_git(clone, "status").returncode == 128
    result = handler().handle(request(registry))
    assert isinstance(result, ModelCoreWorktreeRepairResult)
    assert [r.clone_path for r in result.repairs] == [str(clone)]
    repair = result.repairs[0]
    assert repair.missing_directory == str(missing)
    assert repair.repair_kind == EnumCoreWorktreeRepairKind.UNSET_MISSING_CORE_WORKTREE
    assert repair.outcome == "repaired"
    assert stored(clone) == ""
    assert run_git(clone, "status").returncode == 0
    assert result.failures == 0


def test_core_worktree_naming_an_existing_directory_or_itself_is_left_alone(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = make_clone(registry, "other", str(elsewhere))
    own = registry / "own"
    own_clone = make_clone(registry, "own", str(own))
    plain = make_clone(registry, "plain", None)
    result = handler().handle(request(registry))
    assert result.repairs == ()
    assert result.scanned == 3
    assert stored(other) == str(elsewhere)
    assert stored(own_clone) == str(own)
    assert stored(plain) == ""


def test_core_worktree_relative_value_resolves_against_the_gitdir(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    kept = make_clone(registry, "kept", "..")
    gone = make_clone(registry, "gone", "../no-such-dir")
    result = handler().handle(request(registry))
    assert [r.clone_path for r in result.repairs] == [str(gone)]
    assert result.repairs[0].missing_directory == str(
        (gone / ".git" / "../no-such-dir").resolve()
    )
    assert stored(kept) == ".."


def test_core_worktree_dry_run_detects_and_changes_nothing(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    missing = tmp_path / "gone"
    clone = make_clone(registry, "canonical", str(missing))
    rows = Rows()
    result = handler(rows).handle(request(registry, execute=False))
    assert [r.outcome for r in result.repairs] == ["detected"]
    assert stored(clone) == str(missing)
    assert rows.rows == []


def test_core_worktree_unset_never_lets_git_resolve_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    missing = tmp_path / "gone"
    clone = make_clone(registry, "canonical", str(missing))
    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv: list[str], **kwargs: object) -> object:
        seen.append(list(argv))
        return real_run(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(adapter_core_worktree.subprocess, "run", recording_run)
    handler().handle(request(registry))
    assert seen
    for argv in seen:
        assert argv[:3] == ["git", "config", "--file"]
        assert argv[3] == str(clone / ".git" / "config")
        assert "-C" not in argv
    assert any("--unset" in argv for argv in seen)


def test_core_worktree_repair_writes_one_status_row_naming_clone_directory_and_kind(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    missing = tmp_path / "gone" / "tree"
    clone = make_clone(registry, "canonical", str(missing))
    make_clone(registry, "healthy", None)
    rows = Rows()
    result = handler(rows).handle(request(registry))
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert result.repairs[0].status_row == row
    assert result.repairs[0].row_written is True
    cells = [cell.strip() for cell in row.split(" | ")]
    assert cells[0] == "2026-11-18T12:00:00Z"
    assert cells[1] == "STATUS"
    assert "lane=worktree-reconcile" in cells
    assert "host=h053" in cells
    assert f"clone={clone}" in cells
    assert f"missing_directory={missing}" in cells
    assert "repair_kind=unset_missing_core_worktree" in cells
    assert row.count("\n") == 0


def test_status_row_is_not_written_for_a_clone_left_alone_or_a_failed_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_clone(registry, "alone", str(elsewhere))
    rows = Rows()
    assert handler(rows).handle(request(registry)).repairs == ()
    assert rows.rows == []
    clone = make_clone(registry, "broken", str(tmp_path / "gone"))

    def refuse(self: GitConfigFileCoreWorktree, path: Path) -> None:
        raise RuntimeError("git config failed (255)")

    monkeypatch.setattr(GitConfigFileCoreWorktree, "unset", refuse)
    result = handler(rows).handle(request(registry))
    assert [r.outcome for r in result.repairs] == ["failed"]
    assert result.repairs[0].clone_path == str(clone)
    assert result.failures == 1
    assert rows.rows == []


def test_core_worktree_command_runs_the_repair_from_the_node_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    clone = make_clone(registry, "canonical", str(tmp_path / "gone"))
    code = main(
        [
            "repair-core-worktree",
            "--host",
            "h053",
            "--registry",
            str(registry),
            "--execute",
        ]
    )
    result = ModelCoreWorktreeRepairResult.model_validate_json(
        capsys.readouterr().out.strip().splitlines()[-1]
    )
    assert code == 0
    assert [r.outcome for r in result.repairs] == ["repaired"]
    assert stored(clone) == ""


def test_status_row_writer_runs_the_declared_command_once_per_row(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    make_clone(registry, "one", str(tmp_path / "gone-one"))
    make_clone(registry, "two", str(tmp_path / "gone-two"))
    sink = tmp_path / "sink.txt"
    writer = CommandStatusRowWriter(
        ["sh", "-c", 'printf "%s\\n" "$1" >> "$2"', "append", "{row}", str(sink)]
    )
    result = HandlerCoreWorktreeRepair(
        config=GitConfigFileCoreWorktree(), writer=writer, clock=FixedClock()
    ).handle(request(registry))
    lines = sink.read_text().splitlines()
    assert len(lines) == 2
    assert [r.status_row for r in result.repairs] == lines
    assert all(r.row_written for r in result.repairs)
    with pytest.raises(ValueError, match="row"):
        CommandStatusRowWriter(["true"])


def test_status_row_append_failure_is_reported_and_the_repair_stands(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    clone = make_clone(registry, "canonical", str(tmp_path / "gone"))
    result = HandlerCoreWorktreeRepair(
        config=GitConfigFileCoreWorktree(),
        writer=CommandStatusRowWriter(["false", "{row}"]),
        clock=FixedClock(),
    ).handle(request(registry))
    assert stored(clone) == ""
    assert result.repairs[0].outcome == "repaired"
    assert result.repairs[0].row_written is False
    assert result.repairs[0].error == "row_not_written:RuntimeError"
    assert result.failures == 1
