# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation key store: provider and tenant keys through the secret store node (OMN-21921).

A backend's routing declares the key it needs by reference: a provider
credential (``llm.<provider>.<name>``) or a minted tenant credential
(``cred_<tenant>_<provider>_<uuid4hex>``). This is the ``ProtocolSecretStore``
view the inference resolver reads those references through, and every read is
one ``resolve`` request to :class:`HandlerSecretStore`, so the machine's private
overlay decides the backend: the store the lab hosts run on a lab host, the
machine's own database on a customer laptop. A machine with no overlay block is
read as the local one, which keeps a customer's own key working with no config.

The value moves in process only. A refusal is typed (:class:`SecretStoreRefusedError`)
and an absent value is ``None``, which the resolver turns into its fail-closed
refusal. Nothing here names a store product or a path: the namespaces are the
contract's, and where a deployment keeps a key is the overlay's.
"""

from __future__ import annotations

import re
from pathlib import Path

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretStoreRefusedError,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.adapters import LOCAL_PROVIDER
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

_REFERENCE_PREFIX = service_endpoint_url("internal_targets.secret_reference_prefix")

#: The contract's reference namespaces these two key families map through.
_TENANT_NAMESPACE = "tenant"
_PROVIDER_NAMESPACE = "provider"

#: A contract-declared provider credential, matched by shape so a new backend
#: cannot fall off the rule: ``llm.openrouter.api_key``, ``llm.vertex.access_token``.
_DECLARED_PROVIDER_REF = re.compile(r"^llm\.[A-Za-z0-9_-]+\.[A-Za-z0-9_]+$")


def node_reference_for(api_key_ref: str | None) -> str | None:
    """The ``secret://onex`` reference the node resolves ``api_key_ref`` by, or ``None``.

    ``None`` for a reference that is neither a minted tenant credential nor a
    declared provider credential: a CI token reaches the resolver from a runner
    that has no overlay and never will, and answering it here would widen the
    rule past the credentials it is about.
    """
    if not api_key_ref:
        return None
    if is_tenant_credential_ref(api_key_ref):
        return f"{_REFERENCE_PREFIX}{_TENANT_NAMESPACE}/{api_key_ref}"
    if _DECLARED_PROVIDER_REF.fullmatch(api_key_ref):
        return f"{_REFERENCE_PREFIX}{_PROVIDER_NAMESPACE}/{api_key_ref}"
    return None


def is_node_resolved_ref(api_key_ref: str | None) -> bool:
    """Whether the secret store node, and nothing else, answers this reference."""
    return node_reference_for(api_key_ref) is not None


class NodeSecretStore:
    """Read-only ``ProtocolSecretStore`` over the secret store node's handler."""

    def __init__(
        self,
        handler: HandlerSecretStore | None = None,
        *,
        onex_home: Path | None = None,
    ) -> None:
        self._handler = handler or HandlerSecretStore(
            onex_home=onex_home,
            unconfigured_overlay=ModelSecretStoreOverlay(provider=LOCAL_PROVIDER),
        )

    @property
    def source(self) -> EnumSecretSource:
        """What a value from this machine's store is reported as on the receipt."""
        if self._handler.configured_provider() == LOCAL_PROVIDER:
            return EnumSecretSource.LOCAL_STORE
        return EnumSecretSource.LANE_SECRET_STORE

    async def get_secret(self, key: str) -> str | None:
        reference = node_reference_for(key)
        if reference is None:
            return None
        result = await self._handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.RESOLVE, reference=reference
            )
        )
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return None
        if (
            result.outcome is not EnumSecretStoreOutcome.RESOLVED
            or result.value is None
        ):
            detail = f" ({result.detail})" if result.detail else ""
            raise SecretStoreRefusedError(
                f"Secret reference {key!r} could not be read from this machine's "
                f"secret store: it answered {result.outcome.value}{detail}. "
                "No fallback is permitted.",
                outcome=result.outcome.value,
            )
        return result.value.get_secret_value()

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("The delegation key store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("The delegation key store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


__all__ = ["NodeSecretStore", "is_node_resolved_ref", "node_reference_for"]
