# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab host disk growth (OMN-17427): plugin snapshot eviction and the colocated uv cache.

Observed on the lab hosts, 2026-10-07: ``~/lab-run/plugins`` held one tree per plugin commit with
no eviction (136 snapshots, about 36 GB on one host), and every lane worktree's venv was a full
copy (about 6 GB each) because the uv cache and the worktrees sat on different filesystems, where
a hardlink cannot reach.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
    HandlerLabPluginSnapshotEvict,
    HandlerLabUvCacheColocate,
)
from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
    handler_lab_plugin_snapshot_evict as evict_mod,
)
from omnimarket.nodes.node_lab_disk_growth_effect.models import (
    ModelLabPluginSnapshotEvictRequest,
    ModelLabUvCacheColocateRequest,
)

pytestmark = pytest.mark.unit


def sha(n: int) -> str:
    return f"{n:012x}"


def snapshot(
    plugins: Path, plugin: str, n: int, mtime: float, payload: int = 100
) -> Path:
    """One tree the way the runner leaves it: ``<plugin>-<12 hex>``, newer n is newer mtime."""
    tree = plugins / f"{plugin}-{sha(n)}"
    (tree / "plugins" / "omni").mkdir(parents=True)
    (tree / "plugins" / "omni" / "skill.md").write_bytes(b"x" * payload)
    os.utime(tree, (mtime, mtime))
    return tree


def request(
    plugins: Path, keep_newest: int = 3, dry_run: bool = False
) -> ModelLabPluginSnapshotEvictRequest:
    return ModelLabPluginSnapshotEvictRequest(
        plugins_dir=plugins, keep_newest=keep_newest, dry_run=dry_run
    )


# --- plugin snapshot eviction -----------------------------------------------------------------


def test_keeps_the_newest_three_per_plugin_and_evicts_the_rest(tmp_path: Path) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    other = [snapshot(tmp_path, "otherplugin", n, 1_000_000 + n) for n in (1, 2)]
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
        request(tmp_path)
    )
    assert result.status == "ran"
    assert [p.exists() for p in trees] == [False, False, True, True, True]
    assert all(p.exists() for p in other)
    assert result.evicted == (trees[0].name, trees[1].name)
    assert set(result.kept) == {
        trees[2].name,
        trees[3].name,
        trees[4].name,
        *(o.name for o in other),
    }
    assert result.freed_bytes == 2 * 100
    assert result.errors == ()


def test_a_snapshot_a_running_lane_uses_is_kept_whatever_its_age(
    tmp_path: Path,
) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 7)]
    refs = [
        # a --plugin-dir argument, and a colon list inside one environment value
        f"--plugin-dir={trees[0]}/plugins/omni",
        f"ONEX_SKILL_OVERLAY_ROOTS=/elsewhere:{trees[1]}/overlays:/more",
    ]
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: refs).handle(
        request(tmp_path)
    )
    assert [p.exists() for p in trees] == [True, True, False, True, True, True]
    assert result.evicted == (trees[2].name,)
    assert set(result.kept_in_use) == {trees[0].name, trees[1].name}


def test_a_reference_to_a_longer_name_does_not_hold_a_snapshot(tmp_path: Path) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    refs = [f"{trees[0]}.tmp.4242/plugins", f"{trees[1]}x"]
    HandlerLabPluginSnapshotEvict(process_references=lambda: refs).handle(
        request(tmp_path)
    )
    assert [p.exists() for p in trees] == [False, False, True, True, True]


def test_a_reference_through_a_symlinked_plugins_dir_still_holds(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    trees = [snapshot(real, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    refs = [f"{link}/{trees[0].name}/plugins"]
    HandlerLabPluginSnapshotEvict(process_references=lambda: refs).handle(request(link))
    assert trees[0].exists()
    assert not trees[1].exists()


def test_dry_run_names_what_would_go_and_removes_nothing(tmp_path: Path) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
        request(tmp_path, dry_run=True)
    )
    assert result.status == "dry-run"
    assert result.evicted == (trees[0].name, trees[1].name)
    assert result.freed_bytes == 200
    assert all(p.exists() for p in trees)


def test_an_unreadable_process_table_evicts_nothing(tmp_path: Path) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: None).handle(
        request(tmp_path)
    )
    assert result.status == "skipped"
    assert "process" in result.reason
    assert all(p.exists() for p in trees)


def test_entries_that_are_not_snapshots_are_left_alone(tmp_path: Path) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 5)]
    partial = tmp_path / f"omniclaude-{sha(9)}.tmp.123"
    partial.mkdir()
    stray = tmp_path / "notes.txt"
    stray.write_text("keep")
    link = tmp_path / f"omniclaude-{sha(8)}"
    link.symlink_to(trees[0])
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
        request(tmp_path)
    )
    assert result.evicted == (trees[0].name,)
    assert partial.exists()
    assert stray.exists()
    assert link.is_symlink()


def test_a_tree_is_renamed_away_before_it_is_removed_and_nothing_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane preparing the same commit sees the tree absent (and re-extracts), never half of it."""
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 5)]
    leftover = tmp_path / f"omniclaude-{sha(7)}.evicting.99"
    (leftover / "plugins").mkdir(parents=True)
    seen: list[tuple[str, list[str]]] = []
    real_rmtree = shutil.rmtree

    def spy(path: Path) -> None:
        seen.append((path.name, sorted(p.name for p in tmp_path.iterdir())))
        real_rmtree(path)

    monkeypatch.setattr(evict_mod.shutil, "rmtree", spy)
    HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
        request(tmp_path)
    )
    (_, listing) = next(entry for entry in seen if entry[0].startswith(trees[0].name))
    assert trees[0].name not in listing
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        p.name for p in trees[1:]
    )


def test_keep_newest_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep_newest"):
        HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
            request(tmp_path, keep_newest=0)
        )


def test_a_missing_plugins_dir_is_not_an_error(tmp_path: Path) -> None:
    result = HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
        request(tmp_path / "absent")
    )
    assert result.status == "ran"
    assert result.evicted == ()
    assert result.errors == ()


@pytest.mark.skipif(not Path("/proc/self").is_dir(), reason="needs /proc")
def test_the_process_table_reader_finds_a_snapshot_a_live_process_names(
    tmp_path: Path,
) -> None:
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    env = {**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": f"{trees[0]}/overlays"}
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], env=env
    ) as proc:
        try:
            refs = evict_mod.read_process_references()
            assert refs is not None
            assert any(str(trees[0]) in ref for ref in refs)
            result = HandlerLabPluginSnapshotEvict().handle(request(tmp_path))
        finally:
            proc.kill()
    assert trees[0].exists()
    assert trees[0].name in result.kept_in_use
    assert not trees[1].exists()


# --- uv cache colocated with the worktrees ----------------------------------------------------


def uv_request(tmp: Path, dry_run: bool = False) -> ModelLabUvCacheColocateRequest:
    root = tmp / "omni_worktrees"
    root.mkdir(exist_ok=True)
    return ModelLabUvCacheColocateRequest(
        worktrees_root=root,
        cache_dir=tmp / "state" / "uv-cache",
        uv_config=tmp / "config" / "uv" / "uv.toml",
        dry_run=dry_run,
    )


def uv_handler(
    env: dict[str, str] | None = None, device: Callable[[Path], int] | None = None
) -> HandlerLabUvCacheColocate:
    return HandlerLabUvCacheColocate(
        login_env=lambda: env or {}, device_of=device or (lambda _path: 1)
    )


def test_a_cache_on_the_worktrees_filesystem_is_written_to_the_uv_config(
    tmp_path: Path,
) -> None:
    req = uv_request(tmp_path)
    result = uv_handler().handle(req)
    assert result.status == "configured", result.reason
    assert req.cache_dir.is_dir()
    parsed = tomllib.loads(req.uv_config.read_text())
    assert parsed["cache-dir"] == str(req.cache_dir)
    assert parsed["link-mode"] == "hardlink"
    assert result.env_overrides == ()


def test_a_second_pass_changes_nothing(tmp_path: Path) -> None:
    req = uv_request(tmp_path)
    uv_handler().handle(req)
    before = req.uv_config.read_text()
    result = uv_handler().handle(req)
    assert result.status == "unchanged"
    assert req.uv_config.read_text() == before


def test_the_rest_of_an_existing_uv_config_survives(tmp_path: Path) -> None:
    req = uv_request(tmp_path)
    req.uv_config.parent.mkdir(parents=True)
    req.uv_config.write_text(
        '# operator settings\npython-preference = "managed"\nlink-mode = "copy"\n\n'
        '[pip]\nindex-url = "https://example.invalid/simple"\ncache-dir = "stays"\n'
    )
    result = uv_handler().handle(req)
    assert result.status == "configured"
    parsed = tomllib.loads(req.uv_config.read_text())
    assert parsed["link-mode"] == "hardlink"
    assert parsed["cache-dir"] == str(req.cache_dir)
    assert parsed["python-preference"] == "managed"
    assert parsed["pip"] == {
        "index-url": "https://example.invalid/simple",
        "cache-dir": "stays",
    }
    assert "# operator settings" in req.uv_config.read_text()


def test_a_cache_on_another_filesystem_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    req = uv_request(tmp_path)

    def device(path: Path) -> int:
        return 7 if "omni_worktrees" in str(path) else 9

    result = uv_handler(device=device).handle(req)
    assert result.status == "refused"
    assert "filesystem" in result.reason
    assert not req.uv_config.exists()
    assert not req.cache_dir.exists()


def test_dry_run_writes_and_creates_nothing(tmp_path: Path) -> None:
    req = uv_request(tmp_path, dry_run=True)
    result = uv_handler().handle(req)
    assert result.status == "dry-run"
    assert not req.uv_config.exists()
    assert not req.cache_dir.exists()


def test_an_unparseable_uv_config_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    req = uv_request(tmp_path)
    req.uv_config.parent.mkdir(parents=True)
    req.uv_config.write_text("this is = = not toml [")
    result = uv_handler().handle(req)
    assert result.status == "refused"
    assert req.uv_config.read_text() == "this is = = not toml ["


def test_a_login_profile_that_overrides_the_config_is_reported(tmp_path: Path) -> None:
    req = uv_request(tmp_path)
    env = {"UV_CACHE_DIR": "/scratch/cache/uv", "UV_LINK_MODE": "copy"}
    result = uv_handler(env).handle(req)
    assert result.status == "configured"
    assert result.env_overrides == (
        "UV_CACHE_DIR=/scratch/cache/uv",
        "UV_LINK_MODE=copy",
    )
    assert "uv_env_overrides=2" in result.line()


def test_a_profile_that_already_agrees_is_not_an_override(tmp_path: Path) -> None:
    req = uv_request(tmp_path)
    env = {"UV_CACHE_DIR": str(req.cache_dir), "UV_LINK_MODE": "hardlink"}
    assert uv_handler(env).handle(req).env_overrides == ()


def test_a_missing_worktrees_root_is_refused(tmp_path: Path) -> None:
    req = ModelLabUvCacheColocateRequest(
        worktrees_root=tmp_path / "absent",
        cache_dir=tmp_path / "uv-cache",
        uv_config=tmp_path / "uv.toml",
    )
    assert uv_handler().handle(req).status == "refused"


def test_freed_bytes_count_only_what_the_removal_gives_back(tmp_path: Path) -> None:
    """Snapshots are hardlinked to one another: a file a kept tree still links frees nothing."""
    trees = [snapshot(tmp_path, "omniclaude", n, 1_000_000 + n) for n in range(1, 6)]
    (trees[0] / "shared.bin").write_bytes(b"s" * 1000)
    os.link(
        trees[0] / "shared.bin", trees[4] / "shared.bin"
    )  # a kept tree holds it too
    (trees[1] / "both.bin").write_bytes(b"b" * 700)
    os.link(trees[1] / "both.bin", trees[0] / "both.bin")  # only evicted trees hold it
    for n, tree in enumerate(trees, 1):  # adding files moved the directory times
        os.utime(tree, (1_000_000 + n, 1_000_000 + n))
    for dry_run in (True, False):
        result = HandlerLabPluginSnapshotEvict(process_references=lambda: []).handle(
            request(tmp_path, dry_run=dry_run)
        )
        assert result.evicted == (trees[0].name, trees[1].name)
        # two skill.md files (100 each) and both.bin once; shared.bin stays on disk
        assert result.freed_bytes == 100 + 100 + 700
    assert (trees[4] / "shared.bin").read_bytes() == b"s" * 1000
