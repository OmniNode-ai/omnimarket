# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabReconcileLockProbe -- would the host reconcile lock be takeable here (OMN-26214).

2026-11-20 13:37Z to 14:01Z: three lanes ended blocked on h053 because ``onex-host-reconcile --check`` raised
Errno 30 creating ``.onex-reconcile-host.lock`` under the lane sandbox's read-only registry. The probe is run in
that sandbox before the lane is placed, and reads what creating the lock would raise without doing it: the
filesystem's read-only mount flag and the directory's write permission. It never opens, creates or locks the
lock file, so it cannot hold the real write lock of the host (the check that must itself run on a read-only
root is a separate repair, not this probe).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from typing import Literal

from omnimarket.nodes.node_lab_placement_compute.models.model_lab_admission import (
    ModelLockProbeReading,
    ModelLockProbeRequest,
)


class HandlerLabReconcileLockProbe:
    """EFFECT-free reader: stat the lock directory, never touch the lock."""

    def __init__(
        self,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
        access: Callable[[str, int], bool] = os.access,
    ) -> None:
        self._statvfs = statvfs
        self._access = access

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelLockProbeRequest) -> ModelLockProbeReading:
        lock_dir = request.lock_dir

        def reading(code: int | None, detail: str) -> ModelLockProbeReading:
            return ModelLockProbeReading(lock_dir=lock_dir, errno=code, detail=detail)

        try:
            fs = self._statvfs(lock_dir)
        except FileNotFoundError:
            return reading(errno.ENOENT, "the lock directory does not exist")
        except OSError as exc:
            return reading(exc.errno or errno.EIO, f"statvfs failed: {exc.strerror}")
        if fs.f_flag & os.ST_RDONLY:
            return reading(errno.EROFS, "the lock directory is on a read-only mount")
        if not self._access(lock_dir, os.W_OK):
            return reading(errno.EACCES, "the lock directory is not writable")
        return reading(None, "the lock directory is writable")


__all__ = ["HandlerLabReconcileLockProbe"]
