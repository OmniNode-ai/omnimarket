# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local install's tenant identity, as its mint announced it.

``onex local init`` (and the implicit mint) mint the install's tenant identity
once; the ``local-identity-minted`` event carries that identity and the moment it
was minted, which the API Keys page shows. The event id is derived from those two
fields, so a redelivered event is recognised and changes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

# A fixed namespace, so an event id is the same wherever it is derived.
_EVENT_ID_NAMESPACE = uuid.UUID("0d5e7a3c-4b19-4f86-8c2e-19986a000001")


def local_identity_event_id(tenant_id: uuid.UUID, minted_at: datetime) -> uuid.UUID:
    """Derive a mint event's id from what it says, so a replay carries the same id.

    ``minted_at`` must be aware; it is normalised to UTC first, so the same
    moment written in two offsets derives one id.
    """
    if minted_at.tzinfo is None:
        raise ValueError("minted_at must be an aware datetime")
    name = f"{tenant_id}|{minted_at.astimezone(UTC).isoformat()}"
    return uuid.uuid5(_EVENT_ID_NAMESPACE, name)


class ModelLocalIdentityMinted(BaseModel):
    """The payload of ``local-identity-minted``: one tenant identity, one mint time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    minted_at: AwareDatetime
    event_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _derive_event_id(self) -> ModelLocalIdentityMinted:
        derived = local_identity_event_id(self.tenant_id, self.minted_at)
        if self.event_id is None:
            object.__setattr__(self, "event_id", derived)
        elif self.event_id != derived:
            raise ValueError(
                "event_id does not match the event's tenant id and minted_at"
            )
        return self


__all__ = ["ModelLocalIdentityMinted", "local_identity_event_id"]
