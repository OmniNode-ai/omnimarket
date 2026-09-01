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
* The secret store follows the same ownership rule (OMN-17349): built
  per-request, ``initialize()``d at construction so it can actually write,
  and closed in a ``finally`` -- an authenticated Infisical SDK client must
  not outlive the POST that opened it. An injected store is never closed.
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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from omnimarket.events.topics import (
    CREDENTIAL_REGISTERED_TOPIC_V1,
    CREDENTIAL_REVOKED_TOPIC_V1,
)

_SOURCE_TOOL = "omnimarket-tenant-credential-intake"

# Every variable the Infisical machine identity needs, resolved fail-closed and
# reported together. Deliberately byte-identical to
# ``inference.secret_store_resolver._INFISICAL_BOOTSTRAP_VARS`` (the READ half,
# OMN-16984): the two halves of the BYOK chain must demand the same bootstrap,
# or a host can be configured well enough to accept a customer's key and not
# well enough to hand it back. ``INFISICAL_ENVIRONMENT_SLUG`` is in the REQUIRED
# set rather than defaulted -- see ``_bootstrap_values`` (OMN-17349 AC5).
_INFISICAL_BOOTSTRAP_VARS: tuple[str, ...] = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)

# The folder the tenant-credential values live in. This one keeps a default:
# unlike the environment slug, a wrong path cannot silently cross an environment
# boundary, and the value is a fixed product-level address rather than a
# per-host fact.
_DEFAULT_TENANT_CREDENTIAL_SECRET_PATH = "/tenant-inference-credentials"


class CredentialStoreError(RuntimeError):
    """Base: the BYOK intake store could not be made ready to write.

    Every subclass names ADDRESSING only -- host, project id, environment slug,
    secret path, the minted ref, and the NAMES of bootstrap variables. No secret
    value and no machine-identity material is ever interpolated into any of
    these messages: the onex-api route logs this exception with
    ``logger.exception`` before mapping it to 503, so the message lands in a pod
    log by construction.
    """


class CredentialStoreConfigurationError(CredentialStoreError):
    """The host's Infisical bootstrap configuration is missing or malformed.

    Raised INSTEAD of the bare ``KeyError`` / ``ValidationError`` the previous
    ``os.environ[...]`` triple produced, so a misconfigured host reads as a
    legible fact rather than a traceback (OMN-17349 AC1).
    """


class CredentialStoreUnavailableError(CredentialStoreError):
    """The adapter was configured but could not authenticate against Infisical.

    The underlying ``InfraConnectionError`` is chained as ``__cause__`` (it is
    already sanitized by ``omnibase_infra.utils.util_error_sanitization``), but
    the message raised here is written from addressing this module holds rather
    than forwarded out of the SDK.
    """


class CredentialStoreWriteRejectedError(CredentialStoreError):
    """``set_secret`` reported failure WITHOUT raising -- the key is not stored.

    ``ProtocolSecretStore.set_secret`` is declared ``-> bool`` ("True if stored
    successfully, False otherwise"), so a conforming store is allowed to decline
    a write by returning ``False`` rather than raising. The deployed
    ``InfisicalSecretStore`` only ever returns ``True`` or raises, but this
    publisher is written against the PROTOCOL, not that one implementation.

    Discarding the boolean would publish ``credential-registered`` -- and hand
    the customer a 201 plus an ``api_key_ref`` -- for a key that was never
    persisted. The customer would then see a live credential in the dashboard
    whose every delegation fails to resolve at the effect boundary: exactly the
    "configured and inert" failure class OMN-17349 exists to remove, moved one
    layer out. Fail loud at intake instead.
    """


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


def _bootstrap_values() -> dict[str, str]:
    """Resolve the Infisical bootstrap env fail-closed, naming every gap at once.

    Three deliberate departures from what this replaced:

    * ``INFISICAL_ADDR`` is read HERE rather than via
      ``ModelInfisicalAdapterConfig.host``'s ``default_factory``, whose
      ``os.environ[...]`` raises a bare ``KeyError`` from inside pydantic
      validation.
    * A blank / whitespace-only value counts as missing. An empty
      ``INFISICAL_CLIENT_SECRET`` is a misconfigured host, not a credential.
    * ``INFISICAL_ENVIRONMENT_SLUG`` is REQUIRED (OMN-17349 AC5). It previously
      defaulted to ``"prod"``, so any host that left it unset wrote **customer
      keys into the prod environment of the ``omninode`` project**. The safe
      failure of a secret-write path is to refuse, never to guess the most
      privileged environment.

    Raises:
        CredentialStoreConfigurationError: naming the missing variables. Names
            only -- no value is ever interpolated.
    """
    import os

    values = {
        name: os.environ.get(name, "").strip() for name in _INFISICAL_BOOTSTRAP_VARS
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise CredentialStoreConfigurationError(
            "BYOK credential intake cannot reach the managed secret store: the "
            "Infisical machine identity is not fully configured on this host. "
            f"Missing or blank: {missing}. Required bootstrap variables: "
            f"{list(_INFISICAL_BOOTSTRAP_VARS)}. "
            "INFISICAL_ENVIRONMENT_SLUG has no default by design (OMN-17349): "
            "an unset slug must refuse the write, never pick an environment."
        )
    values["INFISICAL_TENANT_CREDENTIAL_SECRET_PATH"] = (
        os.environ.get("INFISICAL_TENANT_CREDENTIAL_SECRET_PATH", "").strip()
        or _DEFAULT_TENANT_CREDENTIAL_SECRET_PATH
    )
    return values


def _build_secret_store() -> ProtocolSecretStore:
    """Construct an INITIALIZED Infisical-backed store for the value->ref exchange.

    This is the deployed default per OMN-13236 ("wire Infisical-backed
    ProtocolSecretStore as the deployed default"), and the only branch the
    onex-api route ever takes -- it never passes ``secret_store=``.

    ``InfisicalSecretStore`` states its own precondition in its class docstring:
    "The wrapper does not own the adapter's lifecycle; callers must
    ``initialize()`` the adapter before passing it in." This function did not,
    so ``AdapterInfisical`` stayed ``_authenticated = False`` and BOTH write
    paths (``update_secret`` then the ``create_secret`` fallback) raised
    "Infisical adapter not initialized" -- a deterministic 503 for every tenant,
    every provider, every request (OMN-17349). ``handler_secret_seed`` is the
    sibling construction site that gets this right.

    **Ownership model (AC3): the CALLER owns what this returns.** The adapter
    holds an authenticated SDK client, so a per-request construction that is
    never closed leaks one authenticated client per POST.
    :func:`register_inference_credential` closes exactly the store it built, in
    a ``finally``, and never closes an injected one.

    Raises:
        CredentialStoreConfigurationError: bootstrap env missing/malformed.
        CredentialStoreUnavailableError: configured, but ``initialize()`` failed.
    """
    from omnibase_infra.adapters._internal import adapter_infisical
    from omnibase_infra.adapters.models.model_infisical_config import (
        ModelInfisicalAdapterConfig,
    )
    from omnibase_infra.errors import InfraConnectionError
    from omnibase_infra.secret_stores.infisical_secret_store import (
        InfisicalSecretStore,
    )

    values = _bootstrap_values()
    host = values["INFISICAL_ADDR"]
    project_id = values["INFISICAL_PROJECT_ID"]
    environment_slug = values["INFISICAL_ENVIRONMENT_SLUG"]
    secret_path = values["INFISICAL_TENANT_CREDENTIAL_SECRET_PATH"]

    try:
        config = ModelInfisicalAdapterConfig(
            host=host,
            client_id=SecretStr(values["INFISICAL_CLIENT_ID"]),
            client_secret=SecretStr(values["INFISICAL_CLIENT_SECRET"]),
            project_id=uuid.UUID(project_id),
            environment_slug=environment_slug,
            secret_path=secret_path,
        )
    except (ValidationError, ValueError) as exc:
        raise CredentialStoreConfigurationError(
            "BYOK credential intake could not build the managed secret-store "
            f"config for host={host!r} INFISICAL_PROJECT_ID={project_id!r} "
            f"environment_slug={environment_slug!r} secret_path={secret_path!r}. "
            f"Check {list(_INFISICAL_BOOTSTRAP_VARS)}. Underlying error type: "
            f"{type(exc).__name__}."
        ) from exc

    adapter = adapter_infisical.AdapterInfisical(config)
    try:
        adapter.initialize()
    except InfraConnectionError as exc:
        # Release the half-built client rather than leaving an unauthenticated
        # SDK object rooted by the traceback.
        adapter.shutdown()
        raise CredentialStoreUnavailableError(
            "BYOK credential intake could not authenticate against the managed "
            f"secret store host={host!r} project_id={project_id!r} "
            f"environment_slug={environment_slug!r} secret_path={secret_path!r}. "
            "The customer key was NOT stored. Verify the machine identity named "
            f"by {list(_INFISICAL_BOOTSTRAP_VARS)} is live for that project."
        ) from exc

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

    ``api_key_ref`` is minted from the AUTHENTICATED ``tenant_id``, never from
    the request body -- ``ModelInferenceCredentialCreateRequest`` is
    ``extra="forbid"`` and has no ref field, so one tenant structurally cannot
    name (and therefore cannot overwrite) another tenant's secret.

    Store ownership (OMN-17349 AC3) mirrors the ``owns_bus`` shape directly
    below: a store this function CONSTRUCTS holds an authenticated Infisical
    SDK client and is closed in a ``finally``, success or failure, so a
    per-request construction cannot leak one authenticated client per POST. An
    INJECTED store is never closed -- its caller owns its lifetime.

    ``set_secret``'s ``bool`` is CHECKED, not discarded: a protocol-conforming
    store may decline a write by returning ``False``, and publishing
    credential-registered for a key the store does not hold would hand the
    customer a ref that can never resolve.

    Raises:
        CredentialStoreConfigurationError: host bootstrap missing/malformed.
        CredentialStoreUnavailableError: store configured but unauthenticated.
        CredentialStoreWriteRejectedError: the store declined the write.
    """
    api_key_ref = mint_api_key_ref(tenant_id, request.provider)

    owns_store = secret_store is None
    store = secret_store if secret_store is not None else _build_secret_store()
    try:
        stored = await store.set_secret(
            api_key_ref, request.key_value.get_secret_value()
        )
    finally:
        if owns_store:
            await store.close()
    if not stored:
        raise CredentialStoreWriteRejectedError(
            "BYOK credential intake did not persist the key: the managed secret "
            f"store declined the write for ref {api_key_ref!r} (tenant "
            f"{tenant_id!r}, provider {request.provider!r}) by returning False. "
            "No credential-registered event was published -- a ref the store "
            "does not hold must never reach the projection or the customer."
        )

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
