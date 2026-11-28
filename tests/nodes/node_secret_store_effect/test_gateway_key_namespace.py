# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store contract maps ``gateway_key/<name>`` to the ``/tenant-gateway-keys`` folder and key ``<name>``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
    _load_namespaces,
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


def test_load_namespaces_maps_gateway_key_to_its_folder_and_key() -> None:
    namespace = _load_namespaces(_CONTRACT)["gateway_key"]
    assert namespace.folder == "/tenant-gateway-keys"
    assert namespace.key_template.format(name="ci-key") == "ci-key"
    assert namespace.overlay_mapped is False


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


async def test_a_create_by_reference_lands_in_the_gateway_key_folder_create_only() -> (
    None
):
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
    request = ModelSecretStoreRequest(
        operation=EnumSecretStoreOperation.CREATE,
        reference=f"{_reference_prefix()}gateway_key/ci-key",
        value=SecretStr("planted"),
    )
    first = await handler.handle(request)
    assert first.outcome is EnumSecretStoreOutcome.CREATED
    assert (first.folder, first.key) == ("/tenant-gateway-keys", "ci-key")
    assert store.secrets == {"/tenant-gateway-keys/ci-key": "planted"}
    assert (await handler.handle(request)).outcome is EnumSecretStoreOutcome.EXISTS


def test_the_loader_does_not_invent_the_namespace() -> None:
    """Negative control: a namespace the contract does not declare is not in the table."""
    assert "gateway_key_missing" not in _load_namespaces(_CONTRACT)


def _reference_prefix() -> str:
    from omnimarket.config.service_endpoints import service_endpoint_url

    return service_endpoint_url("internal_targets.secret_reference_prefix")
