# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the deploy agent's signed rebuild command on the bus.

``HandlerBranchDeployCommand`` builds the wire envelope from a typed request and the overlay, signs it with the
secret the overlay names from the environment, and publishes it to the topic the contract declares for the deploy
agent's rebuild command. It never runs a trigger script, env file or ssh key: the runtime image holds none of
those. A missing signing key is a typed refusal; a publish error is a typed failure.

Before it publishes, the handler reads the deploy agent's health page and compares the agent's key check value with the
check value of its own signing key (OMN-24396). The deploy agent throws a command signed with another key away without a
job and without a word to the sender (2026-11-14: the dev recovery's first command was lost that way), so a key that
differs, a page that cannot be read, a page with no check value and an overlay that does not configure the read are each a
typed refusal and nothing is published.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_agent_key_check_http import (
    AgentKeyCheckHttpReader,
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

#: The label of the key check value. The deploy agent computes the same value in omnibase_infra's deploy_agent.auth
#: (key_check_value); tests/nodes/node_branch_deploy_command_effect/agent_wire.json holds the golden vector both sides
#: are tested against, so a change on either side fails a test.
KEY_CHECK_LABEL: bytes = b"deploy-agent-key-check-v1"

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


def key_check_value(secret: str) -> str:
    """The one-way check value of a signing key: 16 lowercase hex characters, safe to name in a refusal."""
    return hmac.new(secret.encode(), KEY_CHECK_LABEL, hashlib.sha256).hexdigest()[:16]


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

    def _key_check_reader(self) -> ProtocolAgentKeyCheck | None:
        """The injected reader, else the http reader the overlay configures, else None (not configured)."""
        if self._configured_key_check_reader is not None:
            return self._configured_key_check_reader
        overlay = self._overlay
        if (
            overlay.agent_health_url is None
            or overlay.agent_health_timeout_seconds is None
            or overlay.agent_key_check_field is None
        ):
            return None
        self._configured_key_check_reader = AgentKeyCheckHttpReader(
            overlay.agent_health_url,
            overlay.agent_health_timeout_seconds,
            overlay.agent_key_check_field,
        )
        return self._configured_key_check_reader

    def _refuse(
        self, request: ModelBranchDeployCommandRequest, detail: str
    ) -> ModelBranchDeployCommandResult:
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status="refused",
            detail=detail[: self._overlay.detail_max_chars],
        )

    async def _key_check_refusal(
        self, request: ModelBranchDeployCommandRequest, secret: str
    ) -> ModelBranchDeployCommandResult | None:
        """A refusal when the signing key is not shown to be the deploy agent's key, else None."""
        reader = self._key_check_reader()
        if reader is None:
            return self._refuse(
                request,
                "the overlay names no deploy agent health url, timeout or key check field, so the signing key cannot be "
                "checked against the deploy agent's key; nothing was published",
            )
        reading = await asyncio.to_thread(reader.read)
        if reading.status == "unreachable":
            return self._refuse(
                request,
                f"the deploy agent health page could not be read ({reading.detail}), so the signing key was not "
                "checked; nothing was published",
            )
        if reading.status == "absent":
            return self._refuse(
                request,
                "the deploy agent health page carries no key check value (older agent code, or the agent has no "
                "signing key); nothing was published",
            )
        mine = key_check_value(secret)
        if reading.key_check != mine:
            return self._refuse(
                request,
                f"the signing key does not match the deploy agent's key (signer check {mine}, agent check "
                f"{reading.key_check}); nothing was published",
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
        detail = detail[: overlay.detail_max_chars]
        return ModelBranchDeployCommandResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status="published",
            detail=detail,
        )
