# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a Valkey password through a secret store reference instead of a plain environment variable (OMN-24112).

A launchd unit carries only the reference (the variable named by the overlay's ``password_ref_env``); the password itself
lives in the secret store. A reference that cannot be split, a store that is down or has no identity, or a key the store
does not hold all resolve to an empty password, so a step is never stopped by the reference; the reader then says why it
is disabled (the variable name only, never the reference or a value).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
    from omnimarket.models.secret_store.model_secret_store_overlay import (
        ModelSecretStoreOverlay,
    )

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def split_store_reference(reference: str) -> tuple[str, str]:
    """``secret://onex/<seg>/.../<KEY>`` as ``(folder, key)``; anything else raises a ``ValueError`` that names no part of the reference."""
    if not isinstance(reference, str):
        raise ValueError("a secret store reference is not a string")
    prefix = "secret://onex/"
    if not reference.startswith(prefix):
        raise ValueError("a secret store reference is not a secret://onex path")
    parts = reference[len(prefix) :].split("/")
    if not parts or not parts[-1]:
        raise ValueError("a secret store reference names no key")
    for part in parts:
        if not _SEGMENT.fullmatch(part):
            raise ValueError("a secret store reference segment is not a valid name")
    key = parts[-1]
    folder = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
    return folder, key


def resolve_password(
    reference: str,
    reader: ProtocolSettingStore | None = None,
    store_overlay: ModelSecretStoreOverlay | None = None,
) -> str:
    """The password a reference names, or ``""`` when it cannot be had; when no reader is given it is built from the overlay the cache declares, never raises, never logs or prints."""
    try:
        folder, name = split_store_reference(reference)
    except ValueError:
        return ""
    if reader is None:
        from omnimarket.handlers.delegation_scenario_settings import (
            SecretStoreSettingsReader,
        )

        reader = SecretStoreSettingsReader(overlay=store_overlay)
    try:
        secret = asyncio.run(reader.read(folder, name))
    except Exception:
        return ""
    if secret is None or not secret.get_secret_value():
        return ""
    return secret.get_secret_value()
