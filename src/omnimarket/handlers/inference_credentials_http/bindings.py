# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependency-injection seam of the BYOK credential mount.

The runtime calls the mount factory with no arguments, so what the routes reach
their world through is bound here, once, by whoever wires the runtime profile:
the in-memory profile binds fakes and a test identity, the Redpanda profile
binds the real bus adapter, and the hosted edge binds the identity resolver.
Nothing is bound by default, and an unbound identity resolver refuses every
request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.projection.credential_publisher import ProtocolCredentialEventBus
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from omnimarket.protocols.protocol_inference_credential_http import (
    ProtocolInferenceCredentialReadModel,
    ProtocolRevokeOutbox,
    ProtocolSecretValueSource,
)

#: Logical name of the secret the deterministic ref is keyed with.
REF_HMAC_SECRET_NAME = "inference.credentials.ref_hmac_key"


@dataclass(frozen=True)
class InferenceCredentialBindings:
    """Every port the three routes use. ``None`` means not bound."""

    identity: ProtocolHttpTenantIdentity | None = None
    secret_store: ProtocolSecretStore | None = None
    event_bus: ProtocolCredentialEventBus | None = None
    read_model: ProtocolInferenceCredentialReadModel | None = None
    secret_source: ProtocolSecretValueSource | None = None
    revoke_outbox: ProtocolRevokeOutbox | None = None
    plan_detector: Callable[..., Awaitable[Any]] | None = None
    model_discoverer: Callable[..., Awaitable[Any]] | None = None


_BOUND = InferenceCredentialBindings()


def bind_inference_credential_ports(
    **ports: Any,
) -> InferenceCredentialBindings:
    """Bind (or replace) the named ports; unnamed ports keep their binding."""
    global _BOUND
    _BOUND = replace(_BOUND, **ports)
    return _BOUND


def current_bindings() -> InferenceCredentialBindings:
    """The ports as bound right now (read per request, never cached)."""
    return _BOUND


def reset_inference_credential_ports() -> None:
    """Unbind everything; the mount then refuses every request."""
    global _BOUND
    _BOUND = InferenceCredentialBindings()
