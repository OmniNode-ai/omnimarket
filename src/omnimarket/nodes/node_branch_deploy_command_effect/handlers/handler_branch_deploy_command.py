# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the deploy agent's signed rebuild command on the bus.

``HandlerBranchDeployCommand`` builds the wire envelope from a typed request and the overlay, signs it with the
secret the overlay names from the environment, and publishes it to the topic the contract declares for the deploy
agent's rebuild command. It never runs a trigger script, env file or ssh key: the runtime image holds none of
those. A missing signing key is a typed refusal; a publish error is a typed failure.

When the overlay sets ``check_agent_key``, the handler asks node_deploy_agent_key_check_effect for the deploy agent's key
check value before it publishes and compares it with the check value of its own signing key (OMN-24396, OMN-24413). The
deploy agent throws a command signed with another key away without a job and without a word to the sender (2026-11-14:
the dev recovery's first command was lost that way), so a key that differs, or a check value the node did not serve, is a
typed refusal and nothing is published; a mismatch names both check values in typed result fields (never a key). An
overlay that does not set it publishes as before and says so in the result detail.
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

from omnimarket.events.deploy_agent_key_check import (
    key_check_value,
)
from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_agent_key_check_node import (
    AgentKeyCheckNodeReader,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
    ModelBranchDeployCommandResult,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
    load_overlay,
)
from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_agent_key_check import (
    ProtocolAgentKeyCheck,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.handlers.handler_deploy_agent_key_check import (
    HandlerDeployAgentKeyCheck,
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
        key_check_reader: ProtocolAgentKeyCheck | None = None,
    ) -> None:
        self._bus: Any = event_bus
        self._configured_key_check_reader = key_check_reader
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

    def _key_check_enabled(self) -> bool:
        """True when the overlay asks for the key check or a reader was injected."""
        return (
            self._overlay.check_agent_key is True
            or self._configured_key_check_reader is not None
        )

    def _key_check_reader(self) -> ProtocolAgentKeyCheck:
        """The injected reader, else the key check node's handler reading the same environment as this handler."""
        if self._configured_key_check_reader is None:
            self._configured_key_check_reader = AgentKeyCheckNodeReader(
                HandlerDeployAgentKeyCheck(environ=self._environ),
                "branch-deploy-command",
            )
        return self._configured_key_check_reader

    def _refuse(
        self,
        request: ModelBranchDeployCommandRequest,
        detail: str,
        signer_key_check: str = "",
        agent_key_check: str = "",
    ) -> ModelBranchDeployCommandResult:
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status="refused",
            detail=detail[: self._overlay.detail_max_chars],
            signer_key_check=signer_key_check,
            agent_key_check=agent_key_check,
        )

    async def _key_check_refusal(
        self, request: ModelBranchDeployCommandRequest, secret: str
    ) -> ModelBranchDeployCommandResult | None:
        """A refusal when the signing key is not shown to be the deploy agent's key, else None."""
        if not self._key_check_enabled():
            return None
        reading = await self._key_check_reader().read()
        if reading.status == "refused":
            return self._refuse(
                request,
                f"the deploy agent's key check value was not served ({reading.detail}), so the signing key was not "
                "checked; nothing was published",
            )
        mine = key_check_value(secret)
        if reading.key_check != mine:
            return self._refuse(
                request,
                f"the signing key does not match the deploy agent's key (signer check {mine}, agent check "
                f"{reading.key_check}); nothing was published",
                signer_key_check=mine,
                agent_key_check=reading.key_check,
            )
        return None

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
        if request.overlay_ref:
            envelope["overlay_ref"] = request.overlay_ref
        if request.mode == "deploy":
            envelope["open_pr"] = (
                f"{request.repo}#{request.pr_number}@{request.git_ref}"
            )
        return envelope

    async def handle(
        self, request: ModelBranchDeployCommandRequest
    ) -> ModelBranchDeployCommandResult:
        overlay = self._overlay
        if overlay.hmac_secret_env is None:
            return ModelBranchDeployCommandResult(
                correlation_id=request.correlation_id,
                mode=request.mode,
                status="refused",
                detail="the overlay names no signing key variable (hmac_secret_env is absent)",
            )
        secret = self._environ.get(overlay.hmac_secret_env, "").strip()
        if not secret:
            return ModelBranchDeployCommandResult(
                correlation_id=request.correlation_id,
                mode=request.mode,
                status="refused",
                detail="the deploy agent rejects unsigned commands and the signing key named by the overlay is not set",
            )
        refusal = await self._key_check_refusal(request, secret)
        if refusal is not None:
            return refusal
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
        if not self._key_check_enabled():
            detail += (
                " (the signing key was not checked against the deploy agent's key: "
                "the overlay does not ask for the key check)"
            )
        detail = detail[: overlay.detail_max_chars]
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status="published",
            detail=detail,
        )
