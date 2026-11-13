# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the deploy agent's signed rebuild command on the bus.

``HandlerBranchDeployCommand`` builds the wire envelope from a typed request and the overlay, signs it with the
secret the overlay names from the environment, and publishes it to the topic the contract declares for the deploy
agent's rebuild command. It never runs a trigger script, env file or ssh key: the runtime image holds none of
those. A missing signing key is a typed refusal; a publish error is a typed failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
    ModelBranchDeployCommandResult,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
    load_overlay,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    return loaded


def load_command_topic() -> str:
    """The one topic the contract declares for the deploy agent's rebuild command."""
    topics = _load_contract()["event_bus"]["publish_topics"]
    matching = [
        str(t) for t in topics if str(t).endswith("deploy.rebuild-requested.v1")
    ]
    (topic,) = matching
    return topic


def _sign_envelope(envelope: dict[str, Any], secret: str) -> dict[str, Any]:
    """Sign the envelope with the deploy agent's shared secret (sorted, compact JSON)."""
    body = {k: v for k, v in envelope.items() if k != "_signature"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {
        **body,
        "_signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
    }


class HandlerBranchDeployCommand:
    """Signs the rebuild command and publishes it to the deploy agent over the bus."""

    def __init__(
        self,
        event_bus: Any,
        overlay: ModelBranchDeployCommandOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._bus: Any = event_bus
        self._configured_overlay = overlay
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )
        self._topic = load_command_topic()

    @property
    def _overlay(self) -> ModelBranchDeployCommandOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    def _build_envelope(
        self, request: ModelBranchDeployCommandRequest
    ) -> dict[str, Any]:
        overlay = self._overlay
        envelope: dict[str, Any] = {
            "correlation_id": str(request.correlation_id),
            "requested_by": request.requested_by,
            "scope": overlay.scope,
            "runtime_lane": request.runtime_lane,
            "build_source": overlay.build_source,
            "git_ref": request.git_ref,
            "rollback": {
                "actor": request.rollback_actor,
                "reason": request.rollback_reason,
            },
        }
        if request.image_ref:
            envelope["image_ref"] = request.image_ref
        if request.mode == "deploy":
            envelope["open_pr"] = (
                f"{request.repo}#{request.pr_number}@{request.git_ref}"
            )
        return envelope

    async def handle(
        self, request: ModelBranchDeployCommandRequest
    ) -> ModelBranchDeployCommandResult:
        overlay = self._overlay
        secret = self._environ.get(overlay.hmac_secret_env, "").strip()
        if not secret:
            return ModelBranchDeployCommandResult(
                correlation_id=request.correlation_id,
                mode=request.mode,
                status="refused",
                detail="the deploy agent rejects unsigned commands and the signing key named by the overlay is not set",
            )
        envelope = self._build_envelope(request)
        signed = _sign_envelope(envelope, secret)
        try:
            await self._bus.publish(
                self._topic,
                key=str(request.correlation_id).encode(),
                value=json.dumps(signed).encode(),
            )
        except Exception as exc:
            return ModelBranchDeployCommandResult(
                correlation_id=request.correlation_id,
                mode=request.mode,
                status="failed",
                detail=type(exc).__name__,
            )
        detail = f"published to {self._topic}"
        detail = detail[: overlay.detail_max_chars]
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status="published",
            detail=detail,
        )
