# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin publisher for BYOK inference-credential intake (OMN-16316).

A customer's BYOK submission posts ``{name, provider, key_value}`` to
``POST /v1/tenants/me/inference-credentials``. This module is the *only*
logic behind that route (and its ``DELETE`` sibling): it performs the ONE
value->ref exchange the secret VALUE ever crosses --

    customer TLS request -> this module -> ProtocolSecretStore.set_secret

-- mints a tenant-scoped ``api_key_ref``, calls ``set_secret`` synchronously
at the effect boundary, then publishes a **credential-registered** event
carrying only ``{tenant_id, provider, name, api_key_ref, metadata}`` (no
value, ever) to the canonical topic. Revocation is the mirror shape: it
publishes a **credential-revoked** event and never calls ``delete_secret``
(``InfisicalSecretStore.delete_secret`` always raises per OMN-2286's
read-only policy -- "revoked" means the ref record is deactivated by the
downstream projection, not that the Infisical value is deleted).

Nothing in this module writes to a database, and nothing beyond the single
``set_secret`` call ever holds the plaintext key. The downstream
ingress/projection pair (node_projection_tenant_credentials, OMN-16316
follow-on) is the only thing that persists the ref record.

Design notes (mirrors ``generation_publisher.py``, OMN-13004)
---------------------------------------------------------------
* The event-bus producer is created per-publish and closed immediately
  unless one is injected (tests, or a caller that wants to own the
  lifecycle) -- see ``ProtocolGenerationEventBus`` precedent.
* Fail-fast: a broker or secret store that is unreachable/unconfigured
  raises before any state is left half-written; the route maps that to 503.
* ``ModelInferenceCredentialCreateRequest.key_value`` is a ``pydantic.SecretStr``
  specifically so that any accidental ``repr()``/``str()``/logging of the
  request model prints ``SecretStr('**********')``, never the raw value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omnimarket.events.topics import (
    CREDENTIAL_REGISTERED_TOPIC_V1,
    CREDENTIAL_REVOKED_TOPIC_V1,
)

_SOURCE_TOOL = "omnimarket-tenant-credential-intake"


class ModelInferenceCredentialCreateRequest(BaseModel):
    """Typed body of ``POST /v1/tenants/me/inference-credentials``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Customer-chosen logical label for this credential.",
    )
    provider: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Inference provider id (e.g. 'openrouter', 'openai'). Restricted "
            "to a safe opaque-token charset: provider is interpolated "
            "unencoded into api_key_ref (mint_api_key_ref), which in turn "
            "becomes the Infisical secret path segment and the Kafka message "
            "key -- whitespace, control bytes, or path separators here would "
            "corrupt or path-traverse those downstream identifiers."
        ),
    )
    key_value: SecretStr = Field(
        description=(
            "Raw customer API key. Held in-process only long enough to reach "
            "set_secret(); never logged, never re-serialized, never returned."
        ),
    )


class ModelInferenceCredentialResponse(BaseModel):
    """Response body of the create route -- ref + metadata, NEVER the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    name: str
    provider: str
    created_at: datetime


class ModelInferenceCredentialRevokeResponse(BaseModel):
    """Response body of the revoke route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    status: Literal["revocation-published"] = "revocation-published"


class ModelCredentialRegisteredEvent(BaseModel):
    """Wire shape published on ``CREDENTIAL_REGISTERED_TOPIC_V1``.

    ``extra="forbid"`` is deliberate hardening: this model can never grow an
    accidental ``value``/``key_value`` field without every construction site
    (and ``test_credential_registered_event_never_carries_a_secret_field``)
    breaking loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    provider: str
    name: str
    api_key_ref: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ModelCredentialRevokedEvent(BaseModel):
    """Wire shape published on ``CREDENTIAL_REVOKED_TOPIC_V1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    api_key_ref: str


class ProtocolCredentialEventBus(Protocol):
    """Minimal event-bus seam this publisher needs (satisfied by EventBusKafka).

    Declared locally (identical precedent to ``ProtocolGenerationEventBus``)
    so the publisher depends on behaviour, not a concrete class, and tests
    can inject a fake without a broker.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def publish_envelope(
        self,
        envelope: ModelEventEnvelope[
            ModelCredentialRegisteredEvent | ModelCredentialRevokedEvent
        ],
        topic: str,
        *,
        key: bytes | None = None,
    ) -> None: ...


def mint_api_key_ref(tenant_id: str, provider: str) -> str:
    """Mint a tenant-scoped, collision-safe, opaque credential ref.

    Never caller-supplied (OMN-16316 AC1: "ref generation (tenant-scoped,
    collision-safe)"). The ref carries no secret material -- it is safe to
    log, publish, and display.
    """
    return f"cred_{tenant_id}_{provider}_{uuid.uuid4().hex}"


def _build_event_bus() -> ProtocolCredentialEventBus:
    """Construct the canonical Kafka event bus from resolved bootstrap servers.

    Lazy import mirrors ``generation_publisher._build_event_bus``: importing
    anything from ``omnibase_infra`` transitively loads ``asyncpg``, which the
    projection-api process must never load (OMN-15800 AC6).
    """
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
        ModelKafkaEventBusConfig,
    )

    from omnimarket.config.settings import Settings

    resolved = Settings()
    bootstrap = resolved.get_effective_kafka_bootstrap_servers()
    if not bootstrap:
        raise RuntimeError(
            "KAFKA_BOOTSTRAP_SERVERS (or KAFKA_BROKER) is required to publish a "
            "credential-registered/-revoked event; no broker configured."
        )
    config = ModelKafkaEventBusConfig(bootstrap_servers=bootstrap)
    return cast(ProtocolCredentialEventBus, EventBusKafka(config))


def _build_secret_store() -> ProtocolSecretStore:
    """Construct the Infisical-backed secret store for the value->ref exchange.

    This is the deployed default per OMN-13236 ("wire Infisical-backed
    ProtocolSecretStore as the deployed default"). Fail-fast on missing
    config (CLAUDE.md rule 8) -- a silently-misconfigured store must not
    accept a customer key it cannot actually persist.
    """
    import os

    from omnibase_infra.adapters._internal.adapter_infisical import AdapterInfisical
    from omnibase_infra.adapters.models.model_infisical_config import (
        ModelInfisicalAdapterConfig,
    )
    from omnibase_infra.secret_stores.infisical_secret_store import (
        InfisicalSecretStore,
    )

    config = ModelInfisicalAdapterConfig(
        client_id=SecretStr(os.environ["INFISICAL_CLIENT_ID"]),
        client_secret=SecretStr(os.environ["INFISICAL_CLIENT_SECRET"]),
        project_id=os.environ["INFISICAL_PROJECT_ID"],
        environment_slug=os.environ.get("INFISICAL_ENVIRONMENT_SLUG", "prod"),
        secret_path=os.environ.get(
            "INFISICAL_TENANT_CREDENTIAL_SECRET_PATH", "/tenant-inference-credentials"
        ),
    )
    adapter = AdapterInfisical(config)
    return cast(
        ProtocolSecretStore,
        InfisicalSecretStore(
            adapter,
            project_id=str(config.project_id),
            environment_slug=config.environment_slug,
            secret_path=config.secret_path,
        ),
    )


async def register_inference_credential(
    request: ModelInferenceCredentialCreateRequest,
    *,
    tenant_id: str,
    secret_store: ProtocolSecretStore | None = None,
    event_bus: ProtocolCredentialEventBus | None = None,
) -> ModelInferenceCredentialResponse:
    """Perform the value->ref exchange and thin-publish credential-registered.

    ``key_value`` exists in this process for exactly one line: the
    ``set_secret`` call below. It is never assigned to any other variable,
    never logged, and never placed on the published event.
    """
    api_key_ref = mint_api_key_ref(tenant_id, request.provider)

    store = secret_store or _build_secret_store()
    await store.set_secret(api_key_ref, request.key_value.get_secret_value())

    event = ModelCredentialRegisteredEvent(
        tenant_id=tenant_id,
        provider=request.provider,
        name=request.name,
        api_key_ref=api_key_ref,
    )
    envelope: ModelEventEnvelope[
        ModelCredentialRegisteredEvent | ModelCredentialRevokedEvent
    ] = ModelEventEnvelope(
        payload=event,
        envelope_timestamp=datetime.now(UTC),
        source_tool=_SOURCE_TOOL,
        event_type=CREDENTIAL_REGISTERED_TOPIC_V1,
    )

    owns_bus = event_bus is None
    bus = event_bus or _build_event_bus()
    if owns_bus:
        await bus.start()
    try:
        await bus.publish_envelope(
            envelope,
            CREDENTIAL_REGISTERED_TOPIC_V1,
            key=api_key_ref.encode("utf-8"),
        )
    finally:
        if owns_bus:
            await bus.close()

    return ModelInferenceCredentialResponse(
        api_key_ref=api_key_ref,
        name=request.name,
        provider=request.provider,
        created_at=envelope.envelope_timestamp,
    )


async def revoke_inference_credential(
    api_key_ref: str,
    *,
    tenant_id: str,
    event_bus: ProtocolCredentialEventBus | None = None,
) -> ModelInferenceCredentialRevokeResponse:
    """Thin-publish credential-revoked. Never calls delete_secret (OMN-2286)."""
    event = ModelCredentialRevokedEvent(tenant_id=tenant_id, api_key_ref=api_key_ref)
    envelope: ModelEventEnvelope[
        ModelCredentialRegisteredEvent | ModelCredentialRevokedEvent
    ] = ModelEventEnvelope(
        payload=event,
        envelope_timestamp=datetime.now(UTC),
        source_tool=_SOURCE_TOOL,
        event_type=CREDENTIAL_REVOKED_TOPIC_V1,
    )

    owns_bus = event_bus is None
    bus = event_bus or _build_event_bus()
    if owns_bus:
        await bus.start()
    try:
        await bus.publish_envelope(
            envelope,
            CREDENTIAL_REVOKED_TOPIC_V1,
            key=api_key_ref.encode("utf-8"),
        )
    finally:
        if owns_bus:
            await bus.close()

    return ModelInferenceCredentialRevokeResponse(api_key_ref=api_key_ref)
