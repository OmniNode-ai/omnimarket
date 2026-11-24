# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey kind: every connection value is a bootstrap input, the password is fetched from Infisical."""

from __future__ import annotations

from collections.abc import Mapping

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_valkey import ValkeyOverlayStore
from omnimarket.models.overlay_store.model_store_backend import ModelStoreBackendSpec

ADDRESS_INPUT = "ONEX_SETTINGS_STORE_ADDRESS"
PORT_INPUT = "ONEX_SETTINGS_STORE_PORT"
DATABASE_INPUT = "ONEX_SETTINGS_STORE_DATABASE"
KEY_PREFIX_INPUT = "ONEX_SETTINGS_STORE_KEY_PREFIX"
LOGIN_INPUT = "ONEX_SETTINGS_STORE_LOGIN"
PASSWORD_SECRET = "ONEX_SETTINGS_STORE_PASSWORD"


class BackendValkey:
    spec = ModelStoreBackendSpec(
        kind="store_valkey",
        inputs=(
            ADDRESS_INPUT,
            PORT_INPUT,
            DATABASE_INPUT,
            KEY_PREFIX_INPUT,
            LOGIN_INPUT,
        ),
        secrets=(PASSWORD_SECRET,),
    )

    def build(
        self, inputs: Mapping[str, str], secrets: Mapping[str, str]
    ) -> ProtocolOverlayStore:
        import redis.asyncio

        client = redis.asyncio.Redis(
            host=inputs[ADDRESS_INPUT],
            port=int(inputs[PORT_INPUT]),
            db=int(inputs[DATABASE_INPUT]),
            username=inputs[LOGIN_INPUT],
            password=secrets[PASSWORD_SECRET],
            decode_responses=True,
        )
        return ValkeyOverlayStore(client, key_prefix=inputs[KEY_PREFIX_INPUT])


__all__ = ["BackendValkey"]
