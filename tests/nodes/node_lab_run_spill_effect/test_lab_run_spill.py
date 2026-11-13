# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab run spill (OMN-23384): holes stay holes, pytest temp is skipped, the byte cap stops the spill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_lab_run_spill_effect.handlers.handler_lab_run_spill import (
    HandlerLabRunSpill,
    SpillRefusalError,
    allocated_bytes,
)
from omnimarket.nodes.node_lab_run_spill_effect.models.model_lab_run_spill import (
    ModelLabRunSpillRequest,
    SpillAction,
)
from omnimarket.nodes.node_lab_run_spill_effect.node import NodeLabRunSpillEffect
from omnimarket.nodes.node_lab_run_spill_effect.runtime_lab_run_spill import main

MIB = 1024 * 1024
SPARSE_MIB = 512


def _overlay(path: Path, cap_mib: int = 4096) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "state_suffix": ".ctx",
                "hosts": [
                    {
                        "name": "pool-a",
                        "spill_byte_cap_mib": cap_mib,
                        "skip_names": ["ledger", ".venv", "__pycache__"],
                        "skip_patterns": ["pytest-of-*", "pytest-tmp*"],
                    }
                ],
            }
        )
    )
    return path


def _sparse(path: Path, mib: int = SPARSE_MIB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(mib * MIB)
    assert path.stat().st_blocks * 512 < MIB, "the fixture itself must hold no data"


def _request(
    tmp_path: Path,
    source: Path,
    *,
    action: SpillAction = "spill_state",
    keep_days: int = 0,
) -> ModelLabRunSpillRequest:
    return ModelLabRunSpillRequest(
        action=action,
        host_name="pool-a",
        source=str(source),
        spill_root=str(tmp_path / "spill"),
        overlay_file=str(tmp_path / "overlay.yaml"),
        keep_days=keep_days,
    )


@pytest.fixture
def run_state(tmp_path: Path) -> Path:
    _overlay(tmp_path / "overlay.yaml")
    state = tmp_path / "runs" / "lane-1.ctx"
    state.mkdir(parents=True)
    (state / "result.txt").write_text("done")
    return state


def test_control_copy2_densifies_a_sparse_file(tmp_path: Path, run_state: Path) -> None:
    """The failing control: the copy the upkeep used (``shutil.copy2``) allocates every hole."""
    _sparse(run_state / "fixture.bin")
    handler = HandlerLabRunSpill(copier=shutil.copy2)
    result = handler.handle(_request(tmp_path, run_state))
    copied = tmp_path / "spill" / "lane-1.ctx" / "fixture.bin"
    assert copied.stat().st_blocks * 512 >= SPARSE_MIB * MIB
    assert result.bytes_allocated >= SPARSE_MIB * MIB


def test_sparse_file_stays_sparse_after_the_spill(
    tmp_path: Path, run_state: Path
) -> None:
    fixture = run_state / "fixture.bin"
    _sparse(fixture)
    with fixture.open("r+b") as handle:
        handle.seek(100 * MIB)
        handle.write(b"data-in-the-middle")
    result = HandlerLabRunSpill().handle(_request(tmp_path, run_state))
    copied = tmp_path / "spill" / "lane-1.ctx" / "fixture.bin"
    assert copied.stat().st_size == SPARSE_MIB * MIB
    assert copied.stat().st_blocks * 512 < MIB
    assert result.bytes_allocated < MIB
    assert result.bytes_logical >= SPARSE_MIB * MIB
    with copied.open("rb") as handle:
        handle.seek(100 * MIB)
        assert handle.read(18) == b"data-in-the-middle"
    assert (tmp_path / "spill" / "lane-1.ctx" / "result.txt").read_text() == "done"


def test_pytest_temp_directories_and_declared_names_are_skipped(
    tmp_path: Path, run_state: Path
) -> None:
    for name in ("pytest-of-lane", "pytest-tmp-1", "ledger", ".venv"):
        (run_state / name).mkdir()
        (run_state / name / "x").write_text("x")
    (run_state / "keep").mkdir()
    (run_state / "keep" / "y").write_text("y")
    result = HandlerLabRunSpill().handle(_request(tmp_path, run_state))
    spilled = tmp_path / "spill" / "lane-1.ctx"
    assert sorted(p.name for p in spilled.iterdir()) == ["keep", "result.txt"]
    assert len(result.skipped) == 4


def test_spill_stops_at_the_declared_byte_cap(tmp_path: Path, run_state: Path) -> None:
    _overlay(tmp_path / "overlay.yaml", cap_mib=1)
    for index in range(4):
        (run_state / f"dense-{index}.bin").write_bytes(os.urandom(512 * 1024))
    result = HandlerLabRunSpill().handle(_request(tmp_path, run_state))
    assert result.capped is True
    assert allocated_bytes(tmp_path / "spill") <= MIB
    assert result.spill_root_bytes <= MIB
    assert result.byte_cap == MIB


def test_restore_keeps_holes_and_prune_ages_out(
    tmp_path: Path, run_state: Path
) -> None:
    _sparse(run_state / "fixture.bin", mib=64)
    HandlerLabRunSpill().handle(_request(tmp_path, run_state))
    runs = tmp_path / "fresh-runs"
    runs.mkdir()
    restored = HandlerLabRunSpill().handle(_request(tmp_path, runs, action="restore"))
    back = runs / "lane-1.ctx" / "fixture.bin"
    assert restored.restored == 1
    assert back.stat().st_size == 64 * MIB
    assert back.stat().st_blocks * 512 < MIB
    old = time.time() + 8 * 86400
    pruned = HandlerLabRunSpill(now=lambda: old).handle(
        _request(tmp_path, tmp_path, action="prune", keep_days=7)
    )
    assert pruned.pruned == ("lane-1.ctx",)
    assert not (tmp_path / "spill" / "lane-1.ctx").exists()


def test_golden_chain_node_entry_spills_through_the_overlay(
    tmp_path: Path, run_state: Path
) -> None:
    result = NodeLabRunSpillEffect().handle(_request(tmp_path, run_state))
    assert result.files_copied == 1
    assert result.capped is False
    assert result.errors == ()
    assert (tmp_path / "spill" / "lane-1.ctx" / "result.txt").exists()


def test_golden_chain_command_line_prints_one_result_line(
    tmp_path: Path, run_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "spill_state",
            "--host",
            "pool-a",
            "--source",
            str(run_state),
            "--spill-root",
            str(tmp_path / "spill"),
            "--overlay",
            str(tmp_path / "overlay.yaml"),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["files_copied"] == 1


def test_error_chain_a_host_without_a_row_is_refused(
    tmp_path: Path, run_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(tmp_path, run_state).model_copy(update={"host_name": "pool-z"})
    with pytest.raises(SpillRefusalError, match="no spill is declared"):
        HandlerLabRunSpill().handle(request)
    code = main(
        [
            "spill_state",
            "--host",
            "pool-z",
            "--source",
            str(run_state),
            "--spill-root",
            str(tmp_path / "spill"),
            "--overlay",
            str(tmp_path / "overlay.yaml"),
        ]
    )
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_error_chain_a_spill_past_the_cap_exits_with_the_capped_code(
    tmp_path: Path, run_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _overlay(tmp_path / "overlay.yaml", cap_mib=1)
    (run_state / "big.bin").write_bytes(os.urandom(2 * MIB))
    code = main(
        [
            "spill_state",
            "--host",
            "pool-a",
            "--source",
            str(run_state),
            "--spill-root",
            str(tmp_path / "spill"),
            "--overlay",
            str(tmp_path / "overlay.yaml"),
        ]
    )
    assert code == 4
    assert json.loads(capsys.readouterr().out)["capped"] is True


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@invalid",
            "-C",
            str(path),
            *args,
        ],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def test_preserve_worktree_leaves_a_patch_only_for_an_uncommitted_change(
    tmp_path: Path,
) -> None:
    _overlay(tmp_path / "overlay.yaml")
    dirty, clean = tmp_path / "runs" / "draft-1", tmp_path / "runs" / "clean-1"
    for tree in (dirty, clean):
        tree.mkdir(parents=True)
        _git(tree, "init", "-q")
    (dirty / "a.txt").write_text("one\n")
    _git(dirty, "add", "a.txt")
    _git(dirty, "commit", "-q", "-m", "base")
    (dirty / "a.txt").write_text("two\n")
    handler = HandlerLabRunSpill()
    kept = handler.handle(_request(tmp_path, dirty, action="preserve_worktree"))
    none = handler.handle(_request(tmp_path, clean, action="preserve_worktree"))
    patch = (tmp_path / "spill" / "draft-1.dirty.patch").read_text()
    assert kept.preserved is True
    assert "-one" in patch
    assert "+two" in patch
    assert none.preserved is False
    assert not (tmp_path / "spill" / "clean-1.dirty.patch").exists()
