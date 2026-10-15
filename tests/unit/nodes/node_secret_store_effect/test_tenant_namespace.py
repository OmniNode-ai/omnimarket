# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node contract declares the tenant folder as a reference namespace (OMN-20994).

A caller that registers a tenant's key holds a ``secret://onex/tenant/<ref>``
reference and never a folder string; the contract maps the namespace to the
folder the lane resolver reads tenant references from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

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
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
_REF = "cred_omninode_glm_0123456789abcdef0123456789abcdef"


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


def test_contract_tenant_namespace_declares_the_tenant_folder() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    tenant = raw["reference_namespaces"]["tenant"]
    assert tenant["folder"] == "/tenant-inference-credentials"
    assert tenant["key_template"] == "{name}"


async def test_tenant_namespace_reference_creates_under_the_tenant_folder() -> None:
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
            reference=f"secret://onex/tenant/{_REF}",
            value=SecretStr("planted"),
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert result.folder == "/tenant-inference-credentials"
    assert result.key == _REF
    assert store.secrets == {f"/tenant-inference-credentials/{_REF}": "planted"}
