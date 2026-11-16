# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: the node returns a typed result naming the failing check for a failed sync, an unreadable ref and a missing root."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallResult,
)
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)
from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    environ,
    make_overlay,
    make_root,
    request,
)


def _failing_runner(
    argv: Sequence[str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 127, "", "command not found")


def test_a_failing_sync_rolls_back_and_names_the_sync_check(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    good = NodeUnitTreeInstallEffect(make_overlay(), environ(root))
    assert good.handle(request()).verdict == "pass"
    bad = NodeUnitTreeInstallEffect(make_overlay(), environ(root), _failing_runner)
    result = bad.handle(request())
    assert isinstance(result, ModelUnitTreeInstallResult)
    assert result.verdict == "rolled-back"
    assert next(c.name for c in result.checks if not c.ok) == "sync"
    assert (root / "units" / "u1" / "tree" / "a.txt").exists()


def test_a_first_install_with_a_failing_sync_is_failed_not_rolled_back(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    result = NodeUnitTreeInstallEffect(
        make_overlay(), environ(root), _failing_runner
    ).handle(request())
    assert result.verdict == "failed"
    assert not (root / "units" / "u1" / "record.json").exists()


def test_an_unreadable_ref_is_refused_with_a_build_check_and_installs_nothing(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    result = NodeUnitTreeInstallEffect(make_overlay(), environ(root)).handle(
        request(ref="no-such-ref")
    )
    assert result.verdict == "refused"
    assert [c.name for c in result.checks if not c.ok] == ["build"]
    assert not (root / "units" / "u1" / "tree").exists()
    assert not list((root / "units" / "u1").glob(".*staged*"))


def test_a_missing_root_variable_is_refused_with_a_root_check(tmp_path: Path) -> None:
    make_root(tmp_path)
    result = NodeUnitTreeInstallEffect(make_overlay(), {}).handle(request())
    assert result.verdict == "refused"
    assert [c.name for c in result.checks if not c.ok] == ["root"]
