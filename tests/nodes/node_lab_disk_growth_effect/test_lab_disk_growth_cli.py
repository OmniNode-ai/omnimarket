# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The maintenance CLI exercises real trees without changing them in dry-run mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
    handler_lab_disk_growth_cli as cli,
)

pytestmark = pytest.mark.unit


def test_plugin_snapshots_dry_run_preserves_the_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trees = [tmp_path / f"plugin-{number:012x}" for number in range(4)]
    for tree in trees:
        tree.mkdir()
        (tree / "payload").write_bytes(b"snapshot")
    code = cli.main(["plugin-snapshots", "--plugins-dir", str(tmp_path), "--dry-run"])
    assert code == 0
    status = "dry-run" if Path("/proc/self").is_dir() else "skipped"
    assert capsys.readouterr().out.startswith(f"plugin_snapshots={status}")
    assert all((tree / "payload").read_bytes() == b"snapshot" for tree in trees)
    assert sorted(tmp_path.iterdir()) == trees


def test_uv_cache_dry_run_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    cache = tmp_path / "state" / "cache"
    config = tmp_path / "config" / "uv.toml"
    code = cli.main(
        [
            "uv-cache",
            "--worktrees-root",
            str(root),
            "--cache-dir",
            str(cache),
            "--uv-config",
            str(config),
            "--dry-run",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.startswith("uv_cache=dry-run")
    assert list(tmp_path.iterdir()) == [root]


def test_uv_cache_refusal_returns_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "uv-cache",
            "--worktrees-root",
            str(tmp_path / "missing"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--uv-config",
            str(tmp_path / "uv.toml"),
            "--dry-run",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == "uv_cache=refused\n"
    assert "missing" in captured.err
    assert list(tmp_path.iterdir()) == []
