# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seal a GitHub token to one requester's ephemeral key (OMN-22613).

The git credential node answers on the bus, and the bus keeps what it carries
in its log on disk. So the token never travels in clear: the requester makes an
X25519 key pair in memory for one request and sends only the public half; the
node makes its own one-time pair, derives a key from the two (HKDF-SHA256) and
seals the token with AES-256-GCM. The sealed bytes carry the node's public half,
the nonce and the ciphertext. The repository and the request's correlation id
are bound in as associated data, so a sealed token cannot be presented as the
answer to a different request or repository.

Only the holder of the requester's private half opens it, and that half never
leaves the requesting process's memory.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_INFO: Final[bytes] = b"onex-github-git-credential-v1"
_KEY_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 12
# A public X25519 key is 32 bytes; base64url of 32 bytes is 43 or 44 characters.
MAX_PUBLIC_KEY_CHARS: Final[int] = 64


class SealError(ValueError):
    """A key or sealed value is malformed, or the sealed value does not open."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise SealError("not base64url") from None


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def parse_public_key(text: str) -> X25519PublicKey:
    """The requester's public key from its base64url text, or :class:`SealError`."""
    if len(text) > MAX_PUBLIC_KEY_CHARS:
        raise SealError("public key text is too long")
    raw = _b64decode(text)
    if len(raw) != _KEY_BYTES:
        raise SealError(f"public key is {len(raw)} bytes, not {_KEY_BYTES}")
    return X25519PublicKey.from_public_bytes(raw)


def _aad(correlation_id: UUID, repository: str) -> bytes:
    return correlation_id.bytes + repository.lower().encode("utf-8")


def _derive(shared: bytes, sender: bytes, recipient: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_INFO + sender + recipient,
    ).derive(shared)


@dataclass(frozen=True, repr=False)
class RecipientKeyPair:
    """One request's key pair; the private half lives only in this object."""

    private: X25519PrivateKey

    @classmethod
    def generate(cls) -> RecipientKeyPair:
        return cls(private=X25519PrivateKey.generate())

    @property
    def public_text(self) -> str:
        return _b64encode(_raw_public(self.private.public_key()))

    def open(self, sealed: str, *, correlation_id: UUID, repository: str) -> str:
        """The token inside ``sealed``, or :class:`SealError` if it does not open."""
        raw = _b64decode(sealed)
        if len(raw) <= _KEY_BYTES + _NONCE_BYTES:
            raise SealError("sealed value is too short")
        sender_raw = raw[:_KEY_BYTES]
        nonce = raw[_KEY_BYTES : _KEY_BYTES + _NONCE_BYTES]
        ciphertext = raw[_KEY_BYTES + _NONCE_BYTES :]
        sender = X25519PublicKey.from_public_bytes(sender_raw)
        recipient_raw = _raw_public(self.private.public_key())
        key = _derive(self.private.exchange(sender), sender_raw, recipient_raw)
        try:
            plain = AESGCM(key).decrypt(
                nonce, ciphertext, _aad(correlation_id, repository)
            )
        except InvalidTag:
            raise SealError("sealed value does not open with this key") from None
        return plain.decode("utf-8")


def seal_token(
    token: str,
    recipient: X25519PublicKey,
    *,
    correlation_id: UUID,
    repository: str,
) -> str:
    """Seal ``token`` so only ``recipient``'s private half opens it."""
    sender = X25519PrivateKey.generate()
    sender_raw = _raw_public(sender.public_key())
    key = _derive(sender.exchange(recipient), sender_raw, _raw_public(recipient))
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce, token.encode("utf-8"), _aad(correlation_id, repository)
    )
    return _b64encode(sender_raw + nonce + ciphertext)


__all__ = [
    "MAX_PUBLIC_KEY_CHARS",
    "RecipientKeyPair",
    "SealError",
    "parse_public_key",
    "seal_token",
]
