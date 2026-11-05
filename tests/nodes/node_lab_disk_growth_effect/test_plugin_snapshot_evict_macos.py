# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plugin snapshot eviction on a macOS lab host, which has no /proc.

Recorded case, h101 2026-11-05: ``~/lab-run/plugins`` held 875 snapshots (198 GB) and the
eviction pass could never run there, because it read the process table from /proc and treated a
missing /proc as an unreadable table. On a host without /proc the live references come from
``lsof`` (open files, working directories, executables) and ``ps`` (command lines and
environments); a snapshot one of them names is never evicted, and with neither readable nothing
is evicted.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.host_process_references import (
    path_referenced,
    read_process_references,
)
from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
    HandlerLabPluginSnapshotEvict,
)
from omnimarket.nodes.node_lab_disk_growth_effect.models import (
    ModelLabPluginSnapshotEvictRequest,
)

pytestmark = pytest.mark.unit


def _snapshot(plugins: Path, n: int) -> Path:
    tree = plugins / f"omniclaude-{n:012x}"
    (tree / "plugins" / "omni").mkdir(parents=True)
    (tree / "plugins" / "omni" / "skill.md").write_bytes(b"x" * 10)
    os.utime(tree, (1_000_000 + n, 1_000_000 + n))
    return tree


class _RecordedCommands:
    def __init__(self, outputs: dict[str, tuple[int, str] | None]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str] | None:
        self.calls.append(list(argv))
        return self.outputs[argv[0]]


def test_without_proc_the_references_come_from_lsof_and_ps(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    trees = [_snapshot(plugins, n) for n in range(1, 7)]
    lsof = f"p501\nfcwd\nn{trees[0]}/plugins/omni\np502\nftxt\nn/usr/bin/zsh\n"
    ps = f"claude --plugin-dir {trees[1]}/plugins/omni HOME=/Users/lab\n"
    run = _RecordedCommands({"lsof": (1, lsof), "ps": (0, ps)})

    refs = read_process_references(tmp_path / "no-proc", run=run)

    assert refs is not None
    assert [call[0] for call in run.calls] == ["lsof", "ps"]
    result = HandlerLabPluginSnapshotEvict(
        process_references=lambda: read_process_references(
            tmp_path / "no-proc", run=run
        )
    ).handle(ModelLabPluginSnapshotEvictRequest(plugins_dir=plugins, keep_newest=3))
    assert result.status == "ran"
    assert set(result.kept_in_use) == {trees[0].name, trees[1].name}
    assert set(result.evicted) == {trees[2].name}
    assert trees[0].exists()
    assert trees[1].exists()
    assert not trees[2].exists()


@pytest.mark.parametrize(
    "outputs",
    [
        {"lsof": None, "ps": (0, "zsh\n")},
        {"lsof": (0, "p1\nfcwd\nn/\n"), "ps": None},
        {"lsof": (1, ""), "ps": (0, "zsh\n")},
        {"lsof": (0, "p1\nfcwd\nn/\n"), "ps": (1, "")},
    ],
)
def test_an_unreadable_table_without_proc_still_evicts_nothing(
    tmp_path: Path, outputs: dict[str, tuple[int, str] | None]
) -> None:
    plugins = tmp_path / "plugins"
    trees = [_snapshot(plugins, n) for n in range(1, 6)]
    run = _RecordedCommands(outputs)

    assert read_process_references(tmp_path / "no-proc", run=run) is None
    result = HandlerLabPluginSnapshotEvict(
        process_references=lambda: read_process_references(
            tmp_path / "no-proc", run=run
        )
    ).handle(ModelLabPluginSnapshotEvictRequest(plugins_dir=plugins, keep_newest=3))
    assert result.status == "skipped"
    assert all(tree.exists() for tree in trees)


def test_the_default_reader_falls_back_when_proc_is_absent(tmp_path: Path) -> None:
    """The handler's default reader is the one that knows both process tables."""
    from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
        handler_lab_plugin_snapshot_evict as evict_mod,
    )

    assert evict_mod.read_process_references is read_process_references


def test_a_path_reference_matches_the_tree_and_below_but_not_a_longer_name(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "runs" / "lane-1"
    assert path_referenced(tree, [f"cd {tree}/src && pytest"])
    assert path_referenced(tree, [str(tree)])
    assert not path_referenced(tree, [f"{tree}-2/src"])
    assert not path_referenced(tree, [f"/elsewhere{tree}"])
