# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The transport and key generator seams the dev identity provision handler asks of (OMN-24115).

The transport answers every HTTP status as a response; only when no response arrives does it raise ``ConnectionError``,
naming the exception type alone. The key generator wraps the seed node's generate-key operation. The tenant provisioner
asks the runtime's tenant provision command over the bus and answers with its typed answer, or raises
``TenantProvisionUnavailableError`` when the bus could not be reached or no answer came.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)
from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionAnswer,
    ModelTenantProvisionRequest,
    ModelTenantReadAnswer,
    ModelTenantReadRequest,
)


class TenantProvisionUnavailableError(Exception):
    """The runtime did not answer the tenant provision command: the bus was unreachable or no answer arrived in time."""


@runtime_checkable
class ProtocolDevIdentityTransport(Protocol):
    """The HTTP seam: one request at a time, a response for every status, a ConnectionError when none arrives."""

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse: ...


@runtime_checkable
class ProtocolKeyGenerator(Protocol):
    """The seed node's generate-key operation, asked by the declared key id."""

    async def generate(self, name: str) -> ModelGenerateKeyResult: ...


@runtime_checkable
class ProtocolTenantProvisioner(Protocol):
    """The runtime's tenant provision command: one request, its typed answer, or ``TenantProvisionUnavailableError``."""

    async def provision(
        self, request: ModelTenantProvisionRequest
    ) -> ModelTenantProvisionAnswer: ...


@runtime_checkable
class ProtocolTenantReader(Protocol):
    """The runtime's tenant read command: one request, its typed answer, or ``TenantProvisionUnavailableError``."""

    async def read(self, request: ModelTenantReadRequest) -> ModelTenantReadAnswer: ...


__all__ = [
    "ProtocolDevIdentityTransport",
    "ProtocolKeyGenerator",
    "ProtocolTenantProvisioner",
    "ProtocolTenantReader",
    "TenantProvisionUnavailableError",
]
