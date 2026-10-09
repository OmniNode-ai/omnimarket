# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the copies and removals a migration-sync plan decided (OMN-20687).

Decides nothing: each action is carried out in order, below the vendored root and
nowhere else. An action that cannot be carried out is named in the result and the
others still run, as the old script's per-file ``cp`` and ``rm -f`` did.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

from omnimarket.models.migration_sync import ModelMigrationSyncAction

from ..models import (
    ModelMigrationSyncApplyRequest,
    ModelMigrationSyncApplyResult,
    ModelMigrationSyncFailure,
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_parent_no_follow(root: str, parts: tuple[str, ...], create: bool) -> int:
    """Open the directory holding ``parts[-1]``, never following a symlink.

    Each component is opened relative to the descriptor of the one before it with
    ``O_NOFOLLOW``, so a component swapped for a symlink after any earlier check
    fails the open instead of redirecting the write outside ``root``.
    """
    if create:
        os.makedirs(root, exist_ok=True)
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _copy_no_follow(source: str | Path, name: str, dir_fd: int) -> None:
    """Copy ``source`` to ``name`` under ``dir_fd``, refusing a symlink there."""
    mode = stat.S_IMODE(os.stat(source).st_mode)
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        mode,
        dir_fd=dir_fd,
    )
    with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
        os.fchmod(out.fileno(), mode)
        shutil.copyfileobj(src, out)


class HandlerMigrationSyncApply:
    """Copy and remove vendored migration files below one root."""

    def handle(
        self, request: ModelMigrationSyncApplyRequest
    ) -> ModelMigrationSyncApplyResult:
        root = os.path.realpath(request.dest_root)
        applied: list[ModelMigrationSyncAction] = []
        failed: list[ModelMigrationSyncFailure] = []

        def fail(action: ModelMigrationSyncAction, reason: str) -> None:
            failed.append(
                ModelMigrationSyncFailure(
                    relative_path=action.relative_path, kind=action.kind, reason=reason
                )
            )

        for action in request.actions:
            target = Path(request.dest_root) / action.relative_path
            if os.path.commonpath([root, os.path.realpath(target)]) != root:
                fail(action, f"resolves outside the vendored root {root}")
                continue
            parts = Path(action.relative_path).parts
            try:
                try:
                    dir_fd = _open_parent_no_follow(
                        root, parts, create=action.kind == "copy"
                    )
                except FileNotFoundError:
                    if action.kind == "copy":
                        raise
                    applied.append(action)  # nothing to remove below a missing dir
                    continue
                try:
                    if action.kind == "copy":
                        _copy_no_follow(action.source_path, parts[-1], dir_fd)
                    else:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(parts[-1], dir_fd=dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                fail(action, str(exc))
                continue
            applied.append(action)
        return ModelMigrationSyncApplyResult(
            applied=tuple(applied), failed=tuple(failed)
        )
