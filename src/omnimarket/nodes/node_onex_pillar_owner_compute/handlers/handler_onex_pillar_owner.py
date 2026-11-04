# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure command and cache decisions for the pillar owner wrapper."""

from __future__ import annotations

from pathlib import PurePosixPath

from omnimarket.nodes.node_onex_pillar_owner_compute.models.model_onex_pillar_owner import (
    ModelOnexPillarOwnerRequest,
    ModelOnexPillarOwnerResult,
)


class HandlerOnexPillarOwner:
    """Plan execution without resolving paths, reading the environment or running uv."""

    def handle(
        self, request: ModelOnexPillarOwnerRequest
    ) -> ModelOnexPillarOwnerResult:
        if request.resolve_error is not None:
            return ModelOnexPillarOwnerResult(
                exit_code=2,
                message=request.resolve_error.replace(
                    "onex-ledger resolver", "onex-pillar-owner resolver"
                ),
            )
        if request.uv_path is None:
            return ModelOnexPillarOwnerResult(
                exit_code=127,
                message="onex-pillar-owner resolver: uv is not available on PATH",
            )
        assert request.project_path is not None
        overrides = {}
        if not request.uv_cache_dir:
            overrides["UV_CACHE_DIR"] = str(
                PurePosixPath(request.tempdir_path) / "onex-pillar-owner-uv-cache"
            )
        return ModelOnexPillarOwnerResult(
            command=[
                request.uv_path,
                "run",
                "--project",
                str(PurePosixPath(request.project_path)),
                "onex-pillar-owner",
                *request.argv,
            ],
            env_overrides=overrides,
        )
