# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Removes the entries an entry command names, on this machine, after checking each one (OMN-23867).

Every entry is judged alone and gets one typed row, so one refusal never hides what the others did:

1. the entry must be an absolute path whose fully resolved form (symlinks and ``..`` followed) is
   strictly inside a root the host declares for the command, and must not itself be a symlink;
2. it must exist;
3. nothing may hold it: a live process whose working directory, command line, environment or open file
   names it (the holder scan of ``node_temp_retention_effect``), or, for a worktree, a lease marker
   file the command declares. A process table that cannot be read refuses every entry, because a held
   entry cannot then be ruled out;
4. its tree may not cross onto another device, so a mount point under it is never emptied.

Only then is the entry removed, and the bytes it gave back are counted (a file shared by hard links
counts once). No path of this module's own: the roots and marker names come from the overlay.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageEntryOutcome,
    ModelHostStorageEntryResult,
    ModelHostStorageEntryRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    EnumStorageCommandKind,
    ModelStorageCommand,
    ModelStorageHost,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    read_process_references,
)


class _MountUnderEntryError(OSError):
    """The entry's tree holds a directory on another device."""


def resolve_inside_roots(entry: str, roots: tuple[str, ...]) -> Path | None:
    """The entry's resolved path when it is strictly inside one of the roots, else None.

    The root is resolved too, so a root that is itself reached through a symlink still matches. A
    relative entry is never resolved against anything.
    """
    if not os.path.isabs(entry):
        return None
    resolved = Path(os.path.realpath(entry))
    for root in roots:
        real_root = Path(os.path.realpath(root))
        if resolved != real_root and real_root in resolved.parents:
            return resolved
    return None


def is_held(path: Path, refs: ProcessReferences) -> bool:
    """True when a live process's working directory is in ``path`` or any of its text names ``path``."""
    for directory in refs.directories:
        if directory == path or path in directory.parents:
            return True
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.\-/])" + re.escape(str(path)) + r"(?![A-Za-z0-9_.\-])"
    )
    return any(pattern.search(blob) for blob in refs.blobs)


def is_leased(path: Path, command: ModelStorageCommand) -> str | None:
    """The lease marker file the command declares that is present in the worktree, if any."""
    if command.kind is not EnumStorageCommandKind.REMOVE_WORKTREES:
        return None
    return next((m for m in command.lease_marker_names if (path / m).exists()), None)


def _tree_bytes(path: Path) -> int:
    """Allocated bytes of the tree, each hard-linked file once; raises when a mount point is inside."""
    root_device = path.lstat().st_dev
    seen: set[tuple[int, int]] = set()
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        info = current.lstat()
        if info.st_dev != root_device:
            raise _MountUnderEntryError(str(current))
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            seen.add(key)
            total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            stack.extend(current.iterdir())
    return total


def _make_writable(path: Path) -> None:
    """Add the owner's access to the directories of a tree so a read-only leftover can be removed."""
    uid = os.getuid()
    stack = [path]
    while stack:
        directory = stack.pop()
        with contextlib.suppress(OSError):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                continue
            wanted = stat.S_IMODE(info.st_mode) | stat.S_IRWXU
            if wanted != stat.S_IMODE(info.st_mode):
                directory.chmod(wanted)
            stack.extend(
                Path(e.path)
                for e in os.scandir(directory)
                if e.is_dir(follow_symlinks=False)
            )


def _remove(path: Path) -> None:
    if not path.is_dir():
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        _make_writable(path)
        shutil.rmtree(path)


class EntryRemoverLocal:
    def __init__(
        self,
        process_references: Callable[[], ProcessReferences | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process_references = process_references or read_process_references
        self._clock = clock

    def remove_entries(
        self,
        host: ModelStorageHost,
        command: ModelStorageCommand,
        entries: tuple[str, ...],
    ) -> ModelHostStorageEntryRun:
        roots = host.roots_for(command.name)
        refs = self._process_references()
        deadline = self._clock() + command.timeout_seconds
        rows: list[ModelHostStorageEntryResult] = []
        timed_out = False
        for entry in entries:
            if self._clock() > deadline:
                timed_out = True
                rows.append(
                    ModelHostStorageEntryResult(
                        entry=entry,
                        outcome=EnumHostStorageEntryOutcome.FAILED,
                        detail="the command's timeout passed before this entry",
                    )
                )
                continue
            rows.append(self._one(entry, roots, command, refs))
        return ModelHostStorageEntryRun(entry_results=tuple(rows), timed_out=timed_out)

    def _one(
        self,
        entry: str,
        roots: tuple[str, ...],
        command: ModelStorageCommand,
        refs: ProcessReferences | None,
    ) -> ModelHostStorageEntryResult:
        def row(
            outcome: EnumHostStorageEntryOutcome,
            detail: str | None = None,
            freed: int | None = None,
        ) -> ModelHostStorageEntryResult:
            return ModelHostStorageEntryResult(
                entry=entry, outcome=outcome, freed_bytes=freed, detail=detail
            )

        refused_root = EnumHostStorageEntryOutcome.REFUSED_OUTSIDE_ROOT
        resolved = resolve_inside_roots(entry, roots)
        if resolved is None:
            return row(
                refused_root,
                "not an absolute path strictly inside a declared root once resolved",
            )
        if os.path.islink(entry):
            return row(
                refused_root, "the entry is a symlink; only real entries are removed"
            )
        if not resolved.exists():
            return row(EnumHostStorageEntryOutcome.REFUSED_NOT_FOUND, "no such entry")
        in_use = EnumHostStorageEntryOutcome.REFUSED_IN_USE
        if refs is None:
            return row(
                in_use, "process table unreadable; a held entry cannot be ruled out"
            )
        if is_held(resolved, refs):
            return row(in_use, "a live process holds this entry")
        marker = is_leased(resolved, command)
        if marker is not None:
            return row(in_use, f"leased: {marker} present")
        try:
            freed = _tree_bytes(resolved)
            _remove(resolved)
        except _MountUnderEntryError:
            return row(
                EnumHostStorageEntryOutcome.FAILED,
                "a mount point is inside the entry; nothing was removed",
            )
        except OSError as exc:
            return row(
                EnumHostStorageEntryOutcome.FAILED,
                f"{type(exc).__name__}: {exc.strerror}",
            )
        return row(EnumHostStorageEntryOutcome.REMOVED, freed=freed)
