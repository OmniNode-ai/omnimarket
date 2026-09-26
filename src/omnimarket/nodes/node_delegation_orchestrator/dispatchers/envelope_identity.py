# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer-envelope identities for delegation dispatcher emissions."""

from __future__ import annotations

from uuid import UUID, uuid5

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel


def require_consumed_envelope_id(envelope: object) -> UUID:
    """Return the stable identity of the input that caused an emission.

    A correlation identifies a workflow, not one semantic input.  Direct
    publishers must therefore mint each child from the consumed envelope so a
    new attempt is distinct while redelivery of that exact input is idempotent.
    """
    value: object
    if isinstance(envelope, ModelEventEnvelope):
        value = envelope.envelope_id
    elif isinstance(envelope, dict):
        value = envelope.get("envelope_id")
    else:
        raise ValueError("direct publisher requires a consumed envelope_id")
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError(
                "direct publisher received an invalid envelope_id"
            ) from error
    raise ValueError("direct publisher requires a consumed envelope_id")


def consumed_envelope_tenant_id(envelope: object) -> str | None:
    """Return the tenant recorded on the input envelope, without inventing one."""
    value: object
    if isinstance(envelope, ModelEventEnvelope):
        value = envelope.tenant_id
    elif isinstance(envelope, dict):
        value = envelope.get("tenant_id")
    else:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def emitted_envelope_id(
    *,
    consumed_envelope_id: UUID,
    producer_id: str,
    event: BaseModel,
    index: int,
) -> UUID:
    """Derive an idempotent child identity scoped to one consumed envelope."""
    event_type = type(event)
    return uuid5(
        consumed_envelope_id,
        f"{producer_id}:{event_type.__module__}.{event_type.__qualname__}:{index}",
    )
