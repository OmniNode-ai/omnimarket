# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store contract declares the gateway key folder as a reference namespace (OMN-26475).

``onex tenant key mint --store-dev-secret`` holds a ``gateway_key/<name>`` reference and never a folder string; the
contract maps the namespace to the folder, and the name is the key as it is stored.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.nodes.node_secret_store_effect.handlers import handler_secret_store
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreOverlay,
    ModelSecretStoreRequest,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
_NAME = "acme-test.ci-key.0a1b2c3d"


class _Store:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        if key in self.secrets:
            return False
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def test_load_namespaces_maps_gateway_key_to_its_folder_and_key() -> None:
    namespaces = handler_secret_store._load_namespaces(_CONTRACT)

    gateway_key = namespaces["gateway_key"]
    assert gateway_key.folder == "/tenant-gateway-keys"
    assert gateway_key.key_template == "{name}"
    assert gateway_key.overlay_mapped is False
    assert gateway_key.key_template.format(name=_NAME) == _NAME


async def test_a_gateway_key_reference_creates_under_the_gateway_key_folder() -> None:
    store = _Store()
    handler = HandlerSecretStore(
        store=store,
        overlay=ModelSecretStoreOverlay(
            provider="fake",
            address="http://127.0.0.1:9",
            project_id="00000000-0000-0000-0000-000000000001",
            environment="dev",
            client_id_ref="id-ref",
            client_secret_ref="secret-ref",
        ),
    )

    result = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            reference=f"{service_endpoint_url('internal_targets.secret_reference_prefix')}gateway_key/{_NAME}",
            value=SecretStr("planted"),
        )
    )

    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert result.folder == "/tenant-gateway-keys"
    assert result.key == _NAME
    assert store.secrets == {f"/tenant-gateway-keys/{_NAME}": "planted"}
