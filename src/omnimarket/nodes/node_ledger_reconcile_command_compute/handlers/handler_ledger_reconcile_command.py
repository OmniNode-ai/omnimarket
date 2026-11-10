# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure command and cache decisions for the ledger reconcile wrapper."""

from __future__ import annotations

from pathlib import PurePosixPath

from omnimarket.nodes.node_ledger_reconcile_command_compute.models.model_ledger_reconcile_command import (
    ModelLedgerReconcileCommandRequest,
    ModelLedgerReconcileCommandResult,
)


class HandlerLedgerReconcileCommand:
    """Plan execution without resolving paths, reading the environment or running uv."""

    def handle(
        self, request: ModelLedgerReconcileCommandRequest
    ) -> ModelLedgerReconcileCommandResult:
        if request.resolve_error is not None:
            return ModelLedgerReconcileCommandResult(
                exit_code=2,
                message=request.resolve_error.replace(
                    "onex-ledger resolver", "onex-ledger-reconcile resolver"
                ),
            )
        if request.uv_path is None:
            return ModelLedgerReconcileCommandResult(
                exit_code=127,
                message="onex-ledger-reconcile resolver: uv is not available on PATH",
            )
        assert request.project_path is not None
        overrides = {}
        if not request.uv_cache_dir:
            overrides["UV_CACHE_DIR"] = str(
                PurePosixPath(request.tempdir_path) / "onex-ledger-reconcile-uv-cache"
            )
        return ModelLedgerReconcileCommandResult(
            command=[
                request.uv_path,
                "run",
                "--project",
                str(PurePosixPath(request.project_path)),
                "onex-ledger-reconcile",
                *request.argv,
            ],
            env_overrides=overrides,
        )
