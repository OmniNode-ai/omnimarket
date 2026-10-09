# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the copies and removals a migration-sync plan decided (OMN-20687).

Decides nothing: each action is carried out in order, below the vendored root and
nowhere else. An action that cannot be carried out is named in the result and the
others still run, as the old script's per-file ``cp`` and ``rm -f`` did.
"""

from __future__ import annotations

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


def _copy_no_follow(source: str | Path, target: Path) -> None:
    """Copy ``source`` to ``target``, refusing to open a symlink at ``target``."""
    mode = stat.S_IMODE(os.stat(source).st_mode)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
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
            try:
                if action.kind == "copy":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Re-check after mkdir and refuse a final-component symlink: the
                    # path may have changed since the check above.
                    if os.path.commonpath(
                        [root, os.path.realpath(target.parent)]
                    ) != root or os.path.islink(target):
                        fail(action, f"resolves outside the vendored root {root}")
                        continue
                    _copy_no_follow(action.source_path, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as exc:
                fail(action, str(exc))
                continue
            applied.append(action)
        return ModelMigrationSyncApplyResult(
            applied=tuple(applied), failed=tuple(failed)
        )
