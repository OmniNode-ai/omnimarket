# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed wire shapes for the tenant lifecycle stream (OMN-16930).

``onex-api`` enqueues a tenant lifecycle envelope into ``tenant_event_outbox``
in the SAME transaction that creates the tenant row (OMN-16027), and its
flusher publishes that envelope to ``onex.tenant.events``. This module is the
consumer-side contract for that envelope: it names the fields
``node_projection_tenant_registry`` depends on and refuses anything that does
not carry them.

Why the two models are asymmetric in strictness:

* :class:`ModelTenantRegistryRecord` is ``extra="forbid"``. It is the
  registry identity itself, and an unrecognised key in that object means the
  producer changed the tenant shape without this consumer being updated --
  exactly when a mirror of that shape must refuse to write rather than
  silently persist a partial identity.
* :class:`ModelTenantRegistryEvent` is ``extra="ignore"``. It wraps a full
  ``ModelOnexEnvelope`` serialization, which legitimately carries transport
  fields (correlation ids, timestamps, metadata, schema versions) this
  projection has no interest in. Forbidding those would make the projection
  fail on routine envelope evolution that cannot affect it.

Nothing here defaults an identity. A missing ``tenant_id`` or ``tenant_slug``
raises -- that is the whole point of the relation these events populate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "TENANT_REGISTRY_OPERATIONS",
    "ModelTenantRegistryEvent",
    "ModelTenantRegistryRecord",
    "TenantRegistryEventError",
]

# The operations that carry a full tenant identity. Anything else on the topic
# (billing, quota, deletion-request signalling) is not a registry mutation and
# is ignored by this projection rather than mis-parsed.
TENANT_REGISTRY_OPERATIONS: frozenset[str] = frozenset(
    {"TENANT_CREATED", "TENANT_UPDATED"}
)


class TenantRegistryEventError(ValueError):
    """Raised when a tenant lifecycle envelope cannot yield a registry identity.

    Deliberately a raise rather than a ``None`` return. The mirror this feeds
    is the resolution authority for migration-time identity conversion: a
    partially-written or skipped row does not surface as an error later, it
    surfaces as an unresolvable slug that aborts a migration on some future
    deploy, with the cause several days upstream. Failing at the parse
    boundary keeps the blame local.
    """


class ModelTenantRegistryRecord(BaseModel):
    """One tenant's registry identity, as published by ``onex-api``.

    Field names match the outbox payload verbatim
    (``main.py`` -> ``enqueue_tenant_event`` -> ``payload["tenant"]``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    tenant_slug: str = Field(min_length=1)
    name: str | None = None
    status: str = Field(min_length=1)
    created_at: datetime | None = None
    plan_code: str | None = None

    @field_validator("tenant_slug")
    @classmethod
    def _slug_is_not_whitespace(cls, value: str) -> str:
        """A slug is matched byte-for-byte against a stored ``tenant_id``.

        Whitespace-padding a slug would produce a mirror row that never joins
        to the delegation rows it is supposed to resolve -- a silent
        no-resolution rather than a loud one. Refuse the pad; do not strip it,
        because stripping would invent a binding the producer did not send.
        """
        if value != value.strip():
            raise ValueError(
                f"tenant_slug {value!r} carries leading/trailing whitespace -- "
                "refusing to mirror a slug that cannot match the stored value "
                "byte-for-byte"
            )
        return value


class ModelTenantRegistryEvent(BaseModel):
    """A tenant lifecycle envelope, reduced to what this projection needs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    operation: str = Field(min_length=1)
    tenant: ModelTenantRegistryRecord

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> Self:
        """Parse a raw ``onex.tenant.events`` message.

        Raises :class:`TenantRegistryEventError` for anything that is shaped
        like a tenant event but cannot yield an identity. Callers that want to
        ignore non-registry operations should test ``operation`` against
        :data:`TENANT_REGISTRY_OPERATIONS` first.
        """
        operation = envelope.get("operation")
        if not isinstance(operation, str) or not operation:
            raise TenantRegistryEventError(
                "tenant lifecycle envelope carries no 'operation' -- cannot "
                "decide whether it is a registry mutation"
            )

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise TenantRegistryEventError(
                f"tenant lifecycle envelope {operation!r} carries no object 'payload'"
            )

        tenant = payload.get("tenant")
        if not isinstance(tenant, dict):
            raise TenantRegistryEventError(
                f"tenant lifecycle envelope {operation!r} carries no object "
                "'payload.tenant' -- refusing to mirror an identity this "
                "message does not contain"
            )

        try:
            return cls(operation=operation, tenant=ModelTenantRegistryRecord(**tenant))
        except ValueError as exc:
            raise TenantRegistryEventError(
                f"tenant lifecycle envelope {operation!r} does not carry a "
                f"usable registry identity: {exc}"
            ) from exc
