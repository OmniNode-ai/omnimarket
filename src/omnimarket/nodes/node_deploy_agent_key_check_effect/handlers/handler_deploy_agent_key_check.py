# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answers the one-way check value of the signing key the deploy agent expects (OMN-24413).

The key is read from the environment variable the overlay names (the runtime fills it from the secret store). Only its
check value is returned: the key is in no result, no detail and no log line. A variable that is not set is a typed
refusal, never a guess.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.nodes.node_deploy_agent_key_check_effect.models.model_deploy_agent_key_check import (
    ModelDeployAgentKeyCheckRequest,
    ModelDeployAgentKeyCheckResult,
    key_check_value,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.overlay import (
    ModelDeployAgentKeyCheckOverlay,
    load_overlay,
)


class HandlerDeployAgentKeyCheck:
    """Reads the deploy agent's expected key by reference and answers its check value."""

    def __init__(
        self,
        overlay: ModelDeployAgentKeyCheckOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._configured_overlay = overlay
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    @property
    def _overlay(self) -> ModelDeployAgentKeyCheckOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    async def handle(
        self, request: ModelDeployAgentKeyCheckRequest
    ) -> ModelDeployAgentKeyCheckResult:
        overlay = self._overlay
        secret = self._environ.get(overlay.agent_key_secret_env, "")
        if not secret:
            return ModelDeployAgentKeyCheckResult(
                correlation_id=request.correlation_id,
                status="refused",
                detail=(
                    "the secret named by the overlay is not set, so the deploy agent's key check value cannot be served"
                )[: overlay.detail_max_chars],
            )
        return ModelDeployAgentKeyCheckResult(
            correlation_id=request.correlation_id,
            status="served",
            key_check=key_check_value(secret),
            detail="key check value of the deploy agent's key"[
                : overlay.detail_max_chars
            ],
        )
