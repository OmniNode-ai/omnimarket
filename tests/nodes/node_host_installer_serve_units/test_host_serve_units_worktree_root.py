# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_serve_units: the registry root's omni_worktrees resolves to the host row's declared worktree root (OMN-26371).

Read on h053 at 2026-11-24T00:50Z: ``$OMNI_HOME/omni_worktrees`` is a real directory on the root disk and only two per-lane
symlinks reach the NVMe, so a new lane's worktree lands on the boot disk. The row now declares ``lab_worktree_root`` and the
installer makes the registry root's ``omni_worktrees`` resolve to it, and never moves a tree a lane holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnimarket.handlers.work_root_volume_bar import ModelVolumeReading
from omnimarket.models.overlay_keys import ModelLabHostsTable
from tests.nodes.node_host_installer_serve_units.test_host_serve_units import Host

GIB = 1024**3


def _volume_at(used_gib: int) -> Any:
    def reader(path: Path) -> ModelVolumeReading:
        return ModelVolumeReading(
            path=str(path),
            used_bytes=used_gib * GIB,
            available_bytes=(1000 - used_gib) * GIB,
        )

    return reader


def _table_with_worktree_root(host: Host, root: str | None) -> Any:
    def table() -> ModelLabHostsTable:
        base = host.table().model_dump(mode="json", exclude_none=True)
        if root is not None:
            base["hosts"][0]["lab_worktree_root"] = root
        return ModelLabHostsTable.model_validate(base)

    return table


def _declared(tmp_path: Path) -> tuple[Host, Path]:
    host = Host(tmp_path)
    return host, tmp_path / "nvme" / "lab" / "omni_worktrees"


def test_worktree_root_declared_install_links_the_registry_worktrees_to_the_declared_root(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    link = host.root / "omni_worktrees"
    assert link.is_symlink()
    assert link.resolve() == declared.resolve()
    assert declared.is_dir()
    assert result.worktree_root == "linked"


def test_worktree_root_declared_dry_run_reads_and_writes_nothing(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="dry-run"))
    assert result.status == "success", result.stderr
    assert result.worktree_root == "would-link"
    assert not (host.root / "omni_worktrees").exists()
    assert not declared.exists()


def test_worktree_root_declared_replaces_an_empty_real_directory(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    (host.root / "omni_worktrees").mkdir()
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert (host.root / "omni_worktrees").resolve() == declared.resolve()
    assert result.worktree_root == "linked"


def test_worktree_root_declared_never_moves_a_tree_a_lane_holds(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    tree = host.root / "omni_worktrees" / "OMN-1" / "repo"
    tree.mkdir(parents=True)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.worktree_root == "held: 1 entries under the registry omni_worktrees"
    assert not (host.root / "omni_worktrees").is_symlink()
    assert tree.is_dir()


def test_worktree_root_declared_a_link_to_the_declared_root_is_unchanged(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    declared.mkdir(parents=True)
    (host.root / "omni_worktrees").symlink_to(declared)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.worktree_root == "already-linked"


def test_worktree_root_declared_a_link_elsewhere_is_refused_not_rewritten(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (host.root / "omni_worktrees").symlink_to(other)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "refused"
    assert result.reason == "worktree-root-link-elsewhere"
    assert (host.root / "omni_worktrees").resolve() == other.resolve()


def test_worktree_root_volume_bar_refuses_a_root_on_a_volume_above_the_bar(
    tmp_path: Path,
) -> None:
    host, declared = _declared(tmp_path)
    result = host.handler(
        hosts=_table_with_worktree_root(host, str(declared)), volume=_volume_at(860)
    ).handle(host.request(mode="install"))
    assert result.status == "refused"
    assert result.reason == "work-root-volume-over-bar"
    assert not declared.exists()


def test_worktree_root_declared_a_row_without_one_leaves_the_registry_directory(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    (host.root / "omni_worktrees").mkdir()
    result = host.handler(volume=_volume_at(300)).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.worktree_root == "undeclared"
    assert not (host.root / "omni_worktrees").is_symlink()
