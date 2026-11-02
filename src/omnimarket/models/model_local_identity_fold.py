# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral fold of ``local-identity-minted`` events into one row per tenant.

The identity was minted once, so its row keeps the earliest ``minted_at`` any
mint event announced for that tenant. A replayed event, or a later announcement
of the same identity, writes nothing; an earlier one moves ``minted_at`` back.

``plan_local_identity_write`` is the whole decision, pure, over the rows already
stored; the local SQLite path applies its plan here and the Postgres writer
that follows applies the same plan, so the two stores cannot fold differently.
The read and the write run in one write transaction when the store offers one.

It lives beside the event model, not as a handler, until the projection node's
contract lands and binds a handler that calls it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted
from omnimarket.projection.protocol_database import DatabaseAdapter

LOCAL_IDENTITY_TABLE = "local_identity"
LOCAL_IDENTITY_KEY = "tenant_id"


def _as_datetime(value: object) -> datetime:
    """A stored time: an ISO string from SQLite, a datetime from Postgres."""
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return parsed.astimezone(UTC)


def plan_local_identity_write(
    event: ModelLocalIdentityMinted,
    stored: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[str, object] | None:
    """The row to upsert for ``event``, or ``None`` when it changes nothing."""
    tenant = str(event.tenant_id)
    current = next((row for row in stored if str(row.get("tenant_id")) == tenant), None)
    if current is not None:
        if str(current.get("event_id")) == str(event.event_id):
            return None
        if _as_datetime(current["minted_at"]) <= event.minted_at.astimezone(UTC):
            return None
    return {
        "tenant_id": tenant,
        "minted_at": event.minted_at.astimezone(UTC).isoformat(),
        "event_id": str(event.event_id),
        "updated_at": now.astimezone(UTC).isoformat(),
    }


def apply_local_identity_minted(
    event: ModelLocalIdentityMinted, db: DatabaseAdapter
) -> bool:
    """Fold one mint event through the portable adapter; ``False`` when nothing changed."""
    transaction = getattr(db, "write_transaction", None)
    if transaction is None:
        return _apply(event, db)
    with transaction() as tx:
        return _apply(event, tx)


def _apply(event: ModelLocalIdentityMinted, db: DatabaseAdapter) -> bool:
    stored = db.query(LOCAL_IDENTITY_TABLE, {"tenant_id": str(event.tenant_id)})
    row = plan_local_identity_write(event, stored, datetime.now(UTC))
    if row is None:
        return False
    db.upsert(LOCAL_IDENTITY_TABLE, LOCAL_IDENTITY_KEY, row)
    return True


__all__ = [
    "LOCAL_IDENTITY_KEY",
    "LOCAL_IDENTITY_TABLE",
    "apply_local_identity_minted",
    "plan_local_identity_write",
]
