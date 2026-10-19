# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evict old plugin commits without exposing partially removed snapshot trees."""

from __future__ import annotations

import os
import re
import shutil as shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ..models import (
    ModelLabPluginSnapshotEvictRequest,
    ModelLabPluginSnapshotEvictResult,
)

SNAPSHOT = re.compile(r"^(?P<plugin>.+)-[0-9a-f]{12}$")
EVICTING = re.compile(r"^.+-[0-9a-f]{12}\.evicting\.[0-9]+$")


def read_process_references(proc_root: Path = Path("/proc")) -> list[str] | None:
    """Unreadable items belong to other users; a missing process table blocks eviction."""
    if not (proc_root / "self").is_dir():
        return None
    references: list[str] = []
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit() or not process.is_dir():
            continue
        for name in ("cmdline", "environ"):
            with suppress(OSError):
                references.extend(
                    item.decode(errors="replace")
                    for item in (process / name).read_bytes().split(b"\0")
                    if item
                )
        links = [process / name for name in ("cwd", "root", "exe")]
        with suppress(OSError):
            links.extend((process / "fd").iterdir())
        for link in links:
            with suppress(OSError):
                references.append(os.readlink(link))
    return references


# (device, inode) -> (size, link count, links inside the tree)
_Files = dict[tuple[int, int], tuple[int, int, int]]


def _tree_files(tree: Path) -> _Files:
    """The regular files of a tree by inode: snapshots on a host are hardlinked to one another."""
    files: _Files = {}

    def fail(exc: OSError) -> None:
        raise exc

    for root, dirs, names in os.walk(tree, followlinks=False, onerror=fail):
        for name in (*dirs, *names):
            entry = (Path(root) / name).lstat()
            if stat.S_ISDIR(entry.st_mode):
                continue
            key = (entry.st_dev, entry.st_ino)
            size, links, inside = files.get(key, (entry.st_size, entry.st_nlink, 0))
            files[key] = (size, links, inside + 1)
    return files


def _freed_bytes(removed: list[_Files]) -> int:
    """Bytes the removed trees give back: a file counts once every link to it was removed."""
    total: _Files = {}
    for files in removed:
        for key, (size, links, inside) in files.items():
            _, _, seen = total.get(key, (size, links, 0))
            total[key] = (size, links, seen + inside)
    return sum(size for size, links, seen in total.values() if seen >= links)


def _in_use(tree: Path, plugins_dir: Path, references: list[str]) -> bool:
    for base in {str(plugins_dir), str(plugins_dir.resolve())}:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.\-/])"
            + re.escape(f"{base}/{tree.name}")
            + r"(?![A-Za-z0-9_.\-])"
        )
        if any(pattern.search(reference) for reference in references):
            return True
    return False


class HandlerLabPluginSnapshotEvict:
    """Protect live references before reclaiming any old snapshot."""

    def __init__(
        self, process_references: Callable[[], list[str] | None] | None = None
    ) -> None:
        self.process_references = process_references or read_process_references

    def handle(
        self, request: ModelLabPluginSnapshotEvictRequest
    ) -> ModelLabPluginSnapshotEvictResult:
        if request.keep_newest < 1:
            raise ValueError("keep_newest must be at least 1")
        if not request.plugins_dir.exists():
            return ModelLabPluginSnapshotEvictResult(status="ran")
        references = self.process_references()
        if references is None:
            return ModelLabPluginSnapshotEvictResult(
                status="skipped",
                reason="process table unavailable; live snapshots cannot be checked",
            )
        groups: dict[str, list[Path]] = {}
        errors: list[str] = []
        for tree in sorted(request.plugins_dir.iterdir()):
            if tree.is_symlink() or not tree.is_dir():
                continue
            match = SNAPSHOT.fullmatch(tree.name)
            if match:
                groups.setdefault(match["plugin"], []).append(tree)
            elif EVICTING.fullmatch(tree.name) and not request.dry_run:
                try:
                    shutil.rmtree(tree)
                except OSError as exc:
                    errors.append(f"{tree.name}: {exc}"[:300])
        kept: list[str] = []
        in_use: list[str] = []
        evicted: list[str] = []
        removed: list[_Files] = []
        for plugin in sorted(groups):
            trees = sorted(
                groups[plugin], key=lambda p: (p.stat().st_mtime, p.name), reverse=True
            )
            kept.extend(tree.name for tree in trees[: request.keep_newest])
            older = trees[request.keep_newest :]
            in_use.extend(
                tree.name
                for tree in older
                if _in_use(tree, request.plugins_dir, references)
            )
            for tree in reversed(older):
                if tree.name in in_use:
                    continue
                try:
                    files = _tree_files(tree)
                    if not request.dry_run:
                        renamed = (
                            request.plugins_dir / f"{tree.name}.evicting.{os.getpid()}"
                        )
                        os.rename(tree, renamed)
                        shutil.rmtree(renamed)
                    evicted.append(tree.name)
                    removed.append(files)
                except OSError as exc:
                    errors.append(f"{tree.name}: {exc}"[:300])
        return ModelLabPluginSnapshotEvictResult(
            status="dry-run" if request.dry_run else "ran",
            kept=tuple(kept),
            kept_in_use=tuple(in_use),
            evicted=tuple(evicted),
            freed_bytes=_freed_bytes(removed),
            errors=tuple(errors),
        )
