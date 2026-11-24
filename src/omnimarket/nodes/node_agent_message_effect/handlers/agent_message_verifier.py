# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader side of the sender proof: a message is read only when its declared sender is the registered sender.

Everything the check trusts comes from the injected registry (the settings store): the sender's registered
principal, key id and public key, and the cutover flag. Nothing in the message is trusted but what the registered
key's signature covers.
"""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason as Reason,
)
from omnimarket.handlers.agent_message.signature import verify_signature
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.nodes.node_agent_message_effect.protocols import (
    ProtocolAgentSenderRegistry,
)

SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1})


class AgentMessageVerificationError(ValueError):
    """A message a reader refuses; ``reason`` is the typed cause."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


def classify_validation_error(exc: ValidationError) -> Reason:
    """Oversize body when the body length is the only failure; any other failure is a malformed message."""
    errors = exc.errors()
    if errors and all(
        err["type"] == "string_too_long" and err["loc"][:1] == ("text",)
        for err in errors
    ):
        return Reason.OVERSIZE_BODY
    return Reason.MALFORMED_MESSAGE


def validation_detail(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'message'}: {err['msg']}"
        for err in exc.errors()
    )


class AgentMessageVerifier:
    """Verify one decoded message against the sender registry."""

    def __init__(self, registry: ProtocolAgentSenderRegistry) -> None:
        self._registry = registry

    async def verify(
        self, message: ModelAgentMessage, *, authenticated_principal: str | None = None
    ) -> bool:
        """True when the proof verified; False for a legacy message read before the cutover.

        Raises ``AgentMessageVerificationError`` with the typed reason for every refusal.
        ``authenticated_principal`` is the producer's broker principal when the transport exposes it (the topic's
        records carry none today, so the signature is the proof).
        """
        proof = message.sender_proof
        if proof is None:
            if await self._registry.accepts_legacy():
                return False
            raise AgentMessageVerificationError(
                Reason.MISSING_PROOF,
                f"message from {message.sender!r} carries no sender proof and the cutover is complete",
            )
        if message.message_id is None or message.message_schema_version is None:
            raise AgentMessageVerificationError(
                Reason.MALFORMED_MESSAGE,
                "a signed message carries message_id and message_schema_version",
            )
        if message.message_schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise AgentMessageVerificationError(
                Reason.UNSUPPORTED_SCHEMA_VERSION,
                f"message_schema_version {message.message_schema_version} is not one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
            )
        registered = await self._registry.lookup(message.sender)
        if registered is None:
            raise AgentMessageVerificationError(
                Reason.UNKNOWN_SENDER,
                f"{message.sender!r} is not in the sender registry",
            )
        if (
            authenticated_principal is not None
            and authenticated_principal != registered.principal
        ):
            raise AgentMessageVerificationError(
                Reason.SENDER_MISMATCH,
                f"{message.sender!r} is registered to {registered.principal!r}, "
                f"the producer was {authenticated_principal!r}",
            )
        if proof.key_id != registered.key_id:
            raise AgentMessageVerificationError(
                Reason.SENDER_MISMATCH,
                f"the proof's key {proof.key_id!r} is not {message.sender!r}'s registered key",
            )
        if not verify_signature(message, proof, registered.public_key):
            raise AgentMessageVerificationError(
                Reason.SENDER_MISMATCH,
                f"the signature is not {message.sender!r}'s over this message",
            )
        return True


__all__: list[str] = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "AgentMessageVerificationError",
    "AgentMessageVerifier",
    "classify_validation_error",
    "validation_detail",
]
