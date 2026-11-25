# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant key mint``: make a gateway key in this process and register only its digest over the bus.

The key is the gateway overlay's prefix followed by random characters from the system's source; it is made here, hashed
with sha256 (the gateway hashes a presented key the same way), and the command carries the digest and never the key. The
key leaves this process once, in the result, after the node answered registered. With ``store_dev_secret`` the key is also
written create-only to the dev secret store, allowed only when the command lane, the key node's overlay lane and the
store's environment all match the dev values; every wrong case is refused before the store or the bus is touched, and a
store that already holds the name means no registration.

A dispatch that gets no answer is sent once more with the same digest: a node that stored the key on the first send
answers ``already_registered`` on the resend, and that is reported as registered.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from pydantic import SecretStr

from omnimarket.nodes.node_tenant_cli_effect.handlers import results
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import (
    ContractUnavailableError,
    ModelOperationTopics,
    dev_guard_lane,
    key_topics,
)
from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    ModelTenantCliResult,
    ModelTenantKeyMintRequest,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    ProtocolDevSecretStore,
    ProtocolGatewayKeyOverlays,
    ProtocolTenantDispatch,
    TenantBusUnavailableError,
    TenantOverlayUnavailableError,
)

_COMMAND = "key mint"
OPERATION = "tenant_api_key_register_requested"
ANSWER_EVENT = "ModelTenantApiKeyRegisterResult"
_RANDOM_BYTES: Final[int] = 32
_STORE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_REGISTERED_ON_RESEND: Final[str] = (
    "the first dispatch got no answer and the resend answered already_registered, so the key was registered by the first"
)


class HandlerTenantKeyMint:
    """EFFECT: one register command on the bus (sent again once on no answer), and an optional create-only dev store write."""

    def __init__(
        self,
        dispatch: ProtocolTenantDispatch,
        key_settings: ProtocolGatewayKeyOverlays,
        dev_store: ProtocolDevSecretStore,
    ) -> None:
        self._dispatch = dispatch
        self._overlays = key_settings
        self._dev_store = dev_store

    def _dev_guard_refusal(self) -> ModelTenantCliResult | None:
        """The refusal for a dev secret write that is not on the dev lane, overlay and store, else None. Touches no store or bus."""
        dev_lane = dev_guard_lane()
        command_lane = self._dispatch.lane(key_topics(OPERATION, ANSWER_EVENT).node_id)
        if command_lane != dev_lane:
            return results.refusal(
                _COMMAND,
                "refused_dev_secret_wrong_lane",
                f"--store-dev-secret is allowed only on the {dev_lane} lane; the command goes out on {command_lane!r}",
            )
        if self._overlays.key_node_lane() != dev_lane:
            return results.refusal(
                _COMMAND,
                "refused_dev_secret_wrong_overlay_lane",
                f"--store-dev-secret is allowed only when the gateway key node's overlay lane is {dev_lane}",
            )
        dev_environment = self._overlays.key_node_dev_store_environment()
        if dev_environment is None:
            return results.refusal(
                _COMMAND,
                "refused_dev_secret_not_dev_overlay",
                "--store-dev-secret is allowed only when the gateway key node's overlay names a dev_secret_store_environment",
            )
        if self._dev_store.environment() != dev_environment:
            return results.refusal(
                _COMMAND,
                "refused_dev_secret_wrong_store",
                f"--store-dev-secret is allowed only when this machine's secret store environment is {dev_environment!r}",
            )
        return None

    def _send(
        self, topics: ModelOperationTopics, payload: dict[str, object]
    ) -> dict[str, object] | None:
        return self._dispatch.dispatch(
            node_id=topics.node_id,
            command_topic=topics.requested,
            response_topic=topics.completed,
            payload=dict(payload),
            correlation_in_payload=True,
        )

    def handle(self, request: ModelTenantKeyMintRequest) -> ModelTenantCliResult:
        try:
            topics = key_topics(OPERATION, ANSWER_EVENT)
            if request.store_dev_secret:
                if _STORE_NAME.fullmatch(request.name) is None:
                    return results.refusal(
                        _COMMAND,
                        "refused_dev_secret_invalid_name",
                        "a name stored in the dev secret store is letters, digits, _ . or -",
                    )
                refusal = self._dev_guard_refusal()
                if refusal is not None:
                    return refusal
            prefix = self._overlays.gateway_prefix()
        except ContractUnavailableError as exc:
            return results.node_problem(_COMMAND, "contract_unavailable", str(exc))
        except TenantOverlayUnavailableError as exc:
            return results.node_problem(_COMMAND, "overlay_unavailable", str(exc))

        plaintext = SecretStr(prefix + secrets.token_urlsafe(_RANDOM_BYTES))
        key_hash = hashlib.sha256(
            plaintext.get_secret_value().encode("utf-8")
        ).hexdigest()

        if request.store_dev_secret:
            stored = self._dev_store.create(request.name, plaintext)
            if stored == "exists":
                return results.refusal(
                    _COMMAND,
                    "refused_dev_secret_exists",
                    f"the dev secret store already holds gateway_key/{request.name}; nothing was registered",
                )
            if stored != "created":
                return results.node_problem(
                    _COMMAND,
                    "dev_secret_store_unavailable",
                    f"the dev secret store answered {stored}; nothing was registered",
                )

        payload: dict[str, object] = {
            "tenant_id": str(request.tenant_id),
            "name": request.name,
            "key_hash": key_hash,
        }
        if request.expires_at is not None:
            payload["expires_at"] = request.expires_at.isoformat()

        resent = False
        try:
            answer = self._send(topics, payload)
            if answer is None:
                resent = True
                answer = self._send(topics, payload)
        except TenantBusUnavailableError as exc:
            return results.node_problem(_COMMAND, "bus_unavailable", str(exc))
        if answer is None:
            return results.node_problem(
                _COMMAND,
                "no_answer",
                f"no answer on {topics.completed} to the command or its resend; the key was not printed"
                + (
                    f"; gateway_key/{request.name} holds an unregistered key"
                    if request.store_dev_secret
                    else ""
                ),
            )
        if resent and answer.get("outcome") == "already_registered":
            return results.from_answer(
                _COMMAND,
                {**answer, "outcome": "registered", "detail": _REGISTERED_ON_RESEND},
                plaintext=plaintext,
            )
        return results.from_answer(_COMMAND, answer, plaintext=plaintext)


__all__: list[str] = ["ANSWER_EVENT", "OPERATION", "HandlerTenantKeyMint"]
