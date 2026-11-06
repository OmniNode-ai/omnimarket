# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Link a plugin tree to the store's one venv per lock hash (OMN-22740)."""

from pathlib import Path

from ..models.model_remote_lane_shared_venv import (
    ModelRemoteLaneSharedVenvPruneRequest,
    ModelRemoteLaneSharedVenvPruneResult,
    ModelRemoteLaneSharedVenvRequest,
    ModelRemoteLaneSharedVenvResult,
)


class HandlerRemoteLaneSharedVenv:
    def handle(
        self, request: ModelRemoteLaneSharedVenvRequest
    ) -> ModelRemoteLaneSharedVenvResult:
        from ..plugin_shared_venv import SharedVenvError, ensure_tree_venv

        try:
            done = ensure_tree_venv(
                Path(request.tree),
                Path(request.store_root),
                lock_file=request.lock_file,
                venvs_dir=request.venvs_dir,
                uv=request.uv,
                build_timeout_s=float(request.build_timeout_s),
            )
        except SharedVenvError as exc:
            return ModelRemoteLaneSharedVenvResult(
                ok=False, refusal=exc.code, detail=exc.detail
            )
        return ModelRemoteLaneSharedVenvResult(ok=True, **done)


class HandlerRemoteLaneSharedVenvPrune:
    def handle(
        self, request: ModelRemoteLaneSharedVenvPruneRequest
    ) -> ModelRemoteLaneSharedVenvPruneResult:
        from ..plugin_shared_venv import prune_shared

        return ModelRemoteLaneSharedVenvPruneResult(
            **prune_shared(
                Path(request.store_root),
                plugins_dir=request.plugins_dir,
                venvs_dir=request.venvs_dir,
                retain_days=request.retain_days,
                cap=request.cap,
                build_timeout_s=float(request.build_timeout_s),
            )
        )
