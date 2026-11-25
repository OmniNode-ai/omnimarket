# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sign and verify the typed fields of an agent message with an Ed25519 key.

The writer (the node's send) and every reader (the node's read) call these two functions on the same canonical
bytes, so the signed form cannot drift between them. The signature covers every typed field, the message id and
the schema version; the proof itself is not part of what is signed.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.models.agent_message.model_agent_sender_proof import (
    ModelAgentSenderProof,
)
from omnimarket.models.agent_message.model_agent_signing_key import (
    ModelAgentSigningKey,
)


def canonical_signing_bytes(message: ModelAgentMessage) -> bytes:
    """The bytes a signature covers: every typed field in one sorted compact JSON object."""
    signed = {
        "message_id": message.message_id,
        "from": message.sender,
        "to": message.to,
        "thread": message.thread,
        "kind": message.kind.value,
        "text": message.text,
        "sent_at": message.sent_at.astimezone(UTC).isoformat(),
        "platform": message.platform.value,
        "message_schema_version": message.message_schema_version,
    }
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()


def sign_message(
    message: ModelAgentMessage, key: ModelAgentSigningKey
) -> ModelAgentSenderProof:
    """Sign the message's typed fields with the sender's key from the store."""
    seed = base64.b64decode(key.seed.get_secret_value(), validate=True)
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(
        canonical_signing_bytes(message)
    )
    return ModelAgentSenderProof(
        key_id=key.key_id, signature=base64.b64encode(signature).decode()
    )


def verify_signature(
    message: ModelAgentMessage, proof: ModelAgentSenderProof, public_key_b64: str
) -> bool:
    """True only when the proof's signature is the registered key's over this message's typed fields."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )
        public_key.verify(
            base64.b64decode(proof.signature, validate=True),
            canonical_signing_bytes(message),
        )
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


__all__: list[str] = ["canonical_signing_bytes", "sign_message", "verify_signature"]
