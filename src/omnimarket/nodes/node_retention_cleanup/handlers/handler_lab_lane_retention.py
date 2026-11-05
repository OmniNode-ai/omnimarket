# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retire idle lab runs only after process and Git safety checks pass."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

from omnimarket.handlers.lab_process_references import (
    path_is_referenced,
    read_process_references,
)
from omnimarket.handlers.lab_retention_tree_safety import (
    _git,
    _inside,
    _trees_keep_reason,
)

from ..models.model_file_retention import ModelFileRetentionItemResult
from ..models.model_lab_lane_archive import ModelLabLaneArchiveRequest
from ..models.model_lab_lane_retention import ModelLabLaneRetentionRequest
from .handler_lab_lane_archive import HandlerLabLaneArchive


def _measure(tree: Path) -> tuple[int, int, float]:
    """Bound memory while scanning all files, directories and Git metadata for activity."""
    newest = tree.stat().st_mtime
    files = size = 0

    def fail(exc: OSError) -> None:
        raise exc

    for directory, dirs, names in os.walk(tree, followlinks=False, onerror=fail):
        for name in (*dirs, *names):
            path = Path(directory) / name
            stat = path.lstat()
            newest = max(newest, stat.st_mtime)
            if not path.is_dir() or path.is_symlink():
                files += 1
                size += stat.st_size
    if (tree / ".git").is_file():
        # Linked worktrees keep HEAD, the index and reflogs outside the lane
        # directory. A recent empty commit must reset the idle clock too.
        metadata = Path(_git(tree, "rev-parse", "--absolute-git-dir"))
        newest = max(newest, _measure(metadata)[2])
    return files, size, newest


def _remove_tree(tree: Path) -> None:
    """Let Git unregister linked worktrees; standalone clones own their object store."""
    if (tree / ".git").is_file():
        common = _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.run(
            ["git", "--git-dir", common, "worktree", "remove", str(tree)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    else:
        shutil.rmtree(tree)


class HandlerLabLaneRetention:
    """A missing process table, unreadable tree or uncertain Git state preserves the tree."""

    def __init__(
        self,
        process_references: Callable[[], list[str] | None] | None = None,
    ) -> None:
        self.process_references = process_references or read_process_references

    def handle(
        self, request: ModelLabLaneRetentionRequest
    ) -> ModelFileRetentionItemResult:
        entry = request.entry
        root = request.root.resolve()
        result = ModelFileRetentionItemResult(
            name=entry.name,
            action=entry.action,
            status="dry_run" if request.dry_run else "ok",
        )
        if entry.action != "archive" or root.name != "runs":
            return result.model_copy(
                update={
                    "status": "error",
                    "error": "lab lane safety requires archive under runs",
                }
            )
        references = self.process_references()
        if references is None:
            return result.model_copy(
                update={
                    "status": "error",
                    "error": "process table unavailable; retaining lab runs",
                }
            )
        cutoff = request.now.timestamp() - entry.keep_days * 86400
        kept: dict[str, str] = {}
        before = eligible_files = eligible_bytes = 0
        removed_files = removed_bytes = units = 0
        archived_files = archived_bytes = 0
        for tree in sorted(root.glob(entry.pattern)):
            if any(fnmatch.fnmatch(tree.name, pattern) for pattern in entry.exclude):
                continue
            try:
                _inside(root.parent, tree)
                if tree.parent != root or not tree.is_dir():
                    continue
                context = tree.name.endswith(".ctx")
                sibling = tree.with_name(
                    tree.name[:-4] if context else tree.name + ".ctx"
                )
                # Contexts can own a run's Git object store, credentials and hook
                # inputs. Never expire one while its paired run still exists.
                if context and (sibling.exists() or sibling.is_symlink()):
                    kept[tree.name] = "paired run still exists"
                    continue
                trees = [tree, sibling]
                reference_paths = [
                    request.root.absolute() / path.name for path in trees
                ]
                if any(
                    path_is_referenced(path, references) for path in reference_paths
                ):
                    kept[tree.name] = "live process reference"
                    continue
                files, size, newest = _measure(tree)
                before += size
                paired_newest = _measure(sibling)[2] if sibling.exists() else 0.0
                if max(newest, paired_newest) >= cutoff:
                    kept[tree.name] = "inside idle bound"
                    continue
                if not context and not (tree / ".git").exists():
                    kept[tree.name] = "not a lane repository"
                    continue
                reason = _trees_keep_reason(root.parent, trees)
                if reason:
                    kept[tree.name] = reason
                    continue
                eligible_files += files
                eligible_bytes += size
                if request.dry_run:
                    continue
                # Re-read process, age and Git evidence immediately before removal;
                # failures and any activity since enumeration keep the tree.
                live = self.process_references()
                if live is None or any(
                    path_is_referenced(path, live) for path in reference_paths
                ):
                    kept[tree.name] = "process evidence changed or unavailable"
                    continue
                if (
                    _measure(tree) != (files, size, newest)
                    or (sibling.exists() and _measure(sibling)[2] != paired_newest)
                    or _trees_keep_reason(root.parent, trees)
                ):
                    kept[tree.name] = "tree changed before removal"
                    continue
                saved = HandlerLabLaneArchive().handle(
                    ModelLabLaneArchiveRequest(
                        root=root.parent,
                        run=sibling if context else tree,
                        context=tree if context else sibling,
                        archive_subdir=entry.archive_subdir,
                        head="" if context else _git(tree, "rev-parse", "HEAD"),
                        git_common_dir=""
                        if context
                        else _git(
                            tree,
                            "rev-parse",
                            "--path-format=absolute",
                            "--git-common-dir",
                        ),
                    )
                )
                archived_files += saved.files_archived
                archived_bytes += saved.bytes_archived
                # Archival can take time; recheck the same process and source
                # evidence after readback and immediately before removal.
                live = self.process_references()
                if (
                    live is None
                    or any(path_is_referenced(path, live) for path in reference_paths)
                    or _measure(tree) != (files, size, newest)
                    or (sibling.exists() and _measure(sibling)[2] != paired_newest)
                    or _trees_keep_reason(root.parent, trees)
                ):
                    kept[tree.name] = "activity changed during archival"
                    continue
                _remove_tree(tree)
                removed_files += files
                removed_bytes += size
                units += 1
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                tarfile.TarError,
                EOFError,
            ) as exc:
                kept[tree.name] = f"unreadable or refused: {exc}"[:300]
        return result.model_copy(
            update={
                "files_eligible": eligible_files,
                "bytes_eligible": eligible_bytes,
                "files_removed": removed_files,
                "bytes_removed": removed_bytes,
                "units_removed": units,
                "bytes_before": before,
                "bytes_after": before - removed_bytes,
                "kept": kept,
                "files_archived": archived_files,
                "bytes_archived": archived_bytes,
            }
        )
