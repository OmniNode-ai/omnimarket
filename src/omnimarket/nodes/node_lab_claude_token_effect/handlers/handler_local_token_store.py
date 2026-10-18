# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab token store over the local credential store ``onex secret`` writes to."""

from __future__ import annotations

import asyncio

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore


class HandlerLocalTokenStore:
    """``ProtocolLabTokenStore`` over ``LocalByokCredentialStore`` (mode-0600 local database)."""

    def __init__(self, store: LocalByokCredentialStore | None = None) -> None:
        self._store = store or LocalByokCredentialStore()

    def get(self, ref: str) -> str | None:
        return asyncio.run(self._store.get_secret(ref))

    def put(self, ref: str, value: str) -> None:
        if not asyncio.run(self._store.set_secret(ref, value)):
            raise OSError(f"the local credential store refused a write to {ref}")

    def delete(self, ref: str) -> None:
        asyncio.run(self._store.delete_secret(ref))
