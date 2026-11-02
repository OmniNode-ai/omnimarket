# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which pooled tenant key a routed request runs on, and that key's model.

``omnimarket.handlers.routing.tenant_overlay_resolver`` calls these at the same read
that loads the overlay (OMN-20992, OMN-21217) and re-exports every name here.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from omnimarket.handlers.routing.byok_provider_backends import BYOK_MODEL_UNRESOLVED
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

# The resolver's logger name, so these warnings read exactly as before the move.
logger = logging.getLogger("omnimarket.handlers.routing.tenant_overlay_resolver")


@runtime_checkable
class ProtocolTenantOverlayReader(Protocol):
    """Structural protocol satisfied by ``PostgresReadDatabaseAdapter`` and fakes."""

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        """Return rows from ``table`` matching ``filters``."""
        raise NotImplementedError


#: The tenant's key catalog (node_projection_tenant_credentials, OMN-16316).
#: OMN-20992: the pool the router chooses from. One row per registered key,
#: keyed by ``api_key_ref``, with ``revoked_at`` and (since 0006) ``expires_at``.
TENANT_CREDENTIALS_TABLE = "tenant_inference_credentials"


def _selected_key_model(
    db: ProtocolTenantOverlayReader,
    *,
    tenant_id: str,
    provider: str | None,
    secret_ref: str | None,
    overlay_model: str,
) -> str:
    """Bind a pooled ref to its own choices, keeping unknown keys fail closed.

    No-key overlays retain their distinct credential-absent posture. A house
    overlay with no catalogue entry retains its configured model. A tenant ref
    that cannot prove its own choices keeps that ref and the existing no-model
    sentinel, so the effect refuses it before transport or model discovery.
    """
    if secret_ref is None or provider is None:
        return overlay_model
    try:
        rows = db.query(
            TENANT_CREDENTIALS_TABLE,
            filters={
                "tenant_id": tenant_id,
                "provider": provider,
                "api_key_ref": secret_ref,
            },
        )
    except Exception:
        logger.warning(
            "selected credential choices unreadable for tenant_id=%s",
            tenant_id,
            exc_info=True,
        )
        return (
            BYOK_MODEL_UNRESOLVED
            if is_tenant_credential_ref(secret_ref)
            else overlay_model
        )
    if not rows:
        return (
            BYOK_MODEL_UNRESOLVED
            if is_tenant_credential_ref(secret_ref)
            else overlay_model
        )
    models = rows[0].get("models")
    if (
        isinstance(models, list)
        and models
        and all(
            isinstance(model, str) and model.strip() and len(model) <= 200
            for model in models
        )
        and len(set(models)) == len(models)
    ):
        return str(models[0])
    return BYOK_MODEL_UNRESOLVED


def _select_pool_secret_ref(
    db: ProtocolTenantOverlayReader,
    *,
    tenant_id: str,
    provider: str | None,
    overlay_secret_ref: str | None,
    selector_key: str | None,
    now: datetime,
) -> str | None:
    """Choose the key this request runs on from the tenant's pool (OMN-20992).

    The overlay row names ONE ``secret_ref``: the key registered last, because
    the BYOK bridge overwrites it on every registration. A tenant that holds
    several keys for the routed provider (the company pool, operator ruling
    2026-10-11T16:00:52Z) has them all in ``tenant_inference_credentials``, so
    the choice is made here from that catalog, at the same I/O boundary that
    already read the overlay, and the reducer keeps receiving a plain overlay.

    * Candidates are this tenant's keys for the overlay's provider that are not
      revoked and whose ``expires_at`` is unset or still ahead of ``now``.
      Nothing routes to a lapsed key (ruling 2026-10-11T16:02:35Z).
    * With ``selector_key`` (the request's correlation id) the choice is a hash
      of it over the candidates sorted by ref: stable for one request, spread
      across the pool over many, and independent of the order a read returns.
    * Without one, the overlay's own ref is kept while it is a candidate.
    * Catalogued keys with none live: ``None``. The row is kept, so the tenant
      fails closed at the effect boundary instead of falling back to a house
      key (OMN-17372 ruling 3).
    * No catalogued keys at all (a house overlay row, OMN-19186), no provider on
      the row, or an unreadable catalog: the overlay's ref is returned as read,
      which is exactly the behaviour before the pool existed. An unreadable
      catalog is logged; the provider still refuses a lapsed key, so the cost
      of not filtering here is one failed call, not a call on someone else's
      key.
    """
    if not provider:
        return overlay_secret_ref
    try:
        catalogued = db.query(
            TENANT_CREDENTIALS_TABLE,
            filters={"tenant_id": tenant_id, "provider": provider},
        )
        # A missing or malformed primary key means the catalog could not be
        # read as credentials, not that every registered key has expired.
        # Keep this schema failure inside the existing read-error boundary.
        for row in catalogued:
            ref = row["api_key_ref"]
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("tenant key catalog contains an invalid api_key_ref")
    except Exception:
        logger.warning(
            "tenant key pool read failed for tenant_id=%s provider=%s; routing "
            "on the overlay's own key without pool selection",
            tenant_id,
            provider,
            exc_info=True,
        )
        return overlay_secret_ref
    if not catalogued:
        return overlay_secret_ref
    live = sorted(
        str(row["api_key_ref"])
        for row in catalogued
        if row.get("api_key_ref")
        and row.get("revoked_at") is None
        and _not_expired(row.get("expires_at"), now)
    )
    if not live:
        logger.warning(
            "tenant key pool has no live key: tenant_id=%s provider=%s "
            "catalogued=%d; the route is kept with no key so the call fails "
            "closed",
            tenant_id,
            provider,
            len(catalogued),
        )
        return None
    if selector_key is None:
        return overlay_secret_ref if overlay_secret_ref in live else live[0]
    digest = hashlib.sha256(selector_key.encode("utf-8")).digest()
    return live[int.from_bytes(digest[:8], "big") % len(live)]


def _not_expired(value: object, now: datetime) -> bool:
    """``True`` for an undated key or one whose expiry is still ahead of ``now``.

    Postgres returns a ``datetime``; the local SQLite store returns ISO text.
    A stored value that parses as neither is treated as expired: a date nobody
    can read cannot prove the key still works.
    """
    if value is None:
        return True
    if isinstance(value, datetime):
        expires_at = value
    else:
        try:
            expires_at = datetime.fromisoformat(str(value))
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now
