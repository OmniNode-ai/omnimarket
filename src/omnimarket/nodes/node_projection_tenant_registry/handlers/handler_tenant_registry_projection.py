# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant registry projection: onex.tenant.events -> tenant_registry_mirror (OMN-16930).

The relation this writes is the resolution authority for migration-time
identity conversion. ``0032_delegation_events_tenant_id_uuid_via_registry.sql``
JOINs it in its ``ALTER COLUMN ... TYPE UUID USING`` clause instead of inlining
a literal ``CASE``; a slug the mirror cannot resolve aborts that migration and
says, in the abort message, that THIS projection has not caught up.

That gives the writer here an unusual property worth stating plainly: a row
this projection declines to write does not fail here, it fails on some later
deploy, in a different repo, with the cause days upstream. Every branch below
therefore raises rather than logging-and-continuing. There is no
"skip the malformed one and keep going" path.

Only the runtime touches the database (``feedback_only_runtime_touches_database``).
``onex-api`` owns the registry and publishes it; it never writes
``omnidash_analytics``. This node never reads ``omninode_cloud``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_registry_events import (
    TENANT_REGISTRY_OPERATIONS,
    ModelTenantRegistryEvent,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset({"tenant_registry_mirror"})


class TenantIdentityRebindingError(ValueError):
    """Raised when an event rebinds a known slug to a DIFFERENT tenant UUID.

    Silently accepting a rebinding would retroactively change the identity
    every past conversion resolved through this slug -- reassigning one
    customer's historical rows to another tenant. That is the exact class of
    defect OMN-15683 exists to close, so it is refused here rather than
    upserted. A legitimate re-key is an operator action with its own ticket,
    not a stream event.
    """


class HandlerTenantRegistryProjectionRunner(BaseProjectionRunner):
    """Projects tenant lifecycle events into ``tenant_registry_mirror``."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not "
                    "in KNOWN_PROJECTION_TABLES"
                )

        if "registry_mirror" not in _by_role:
            raise ValueError("Contract missing required table role 'registry_mirror'")

        self._table_mirror: str = _by_role["registry_mirror"]

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal def-B handler entrypoint (OMN-14355 canonical shape).

        ``request`` is the magic single-positional-param name the shared
        ``runtime_local_adapter`` recognizes and adapts. This handler takes a
        typed payload and returns a typed result -- it stays clear of the
        pre-def-B bus-envelope type and never returns a wrapped handler
        output (OMN-14355 C-core: no envelope reference in the handler core).
        """
        topics = self.subscribe_topics
        topic = str(request.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(request.pop("_partition", 0)),
            offset=int(request.pop("_offset", 0)),
            fallback_id=str(request.pop("_fallback_id", "")),
            topic=topic,
        )
        projected = asyncio.run(self.project_event(topic, request, meta))
        return {"projected": projected}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        del topic  # single-topic node; routing is by operation, not by topic

        operation = data.get("operation")
        if (
            not isinstance(operation, str)
            or operation not in TENANT_REGISTRY_OPERATIONS
        ):
            # Not a registry mutation. onex.tenant.events is a shared
            # control-plane topic; declining an operation this node does not
            # own is correct, and is NOT the same as declining a malformed
            # registry event (which raises, below).
            logger.debug(
                "tenant lifecycle operation %r is not a registry mutation; ignoring",
                operation,
            )
            return True

        # Raises TenantRegistryEventError on anything shaped like a registry
        # mutation that cannot yield an identity. Deliberately not caught:
        # a dropped identity surfaces days later as an aborted migration.
        event = ModelTenantRegistryEvent.from_envelope(data)
        tenant = event.tenant

        # Refuse a rebinding BEFORE the upsert, so a refused write leaves zero
        # rows changed rather than a half-applied identity.
        existing = await self.db.execute(
            f"SELECT tenant_uuid FROM {self._table_mirror} WHERE tenant_slug = $1",
            tenant.tenant_slug,
        )
        if existing and str(existing[0]["tenant_uuid"]) != str(tenant.tenant_id):
            raise TenantIdentityRebindingError(
                f"tenant slug {tenant.tenant_slug!r} is already mirrored as "
                f"{existing[0]['tenant_uuid']} and this {event.operation} event "
                f"binds it to {tenant.tenant_id}. Refusing to rebind: every "
                "identity conversion that has already resolved through this "
                "slug would retroactively point at a different tenant. A "
                "legitimate re-key is an operator action with its own ticket, "
                "not a stream event (OMN-16930)."
            )

        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_mirror} (
              tenant_slug, tenant_uuid, display_name, status,
              registry_created_at, observed_at, source_event_id
            ) VALUES (
              $1, $2, $3, $4, $5, NOW(), $6
            )
            ON CONFLICT (tenant_slug) DO UPDATE SET
              display_name = EXCLUDED.display_name,
              status = EXCLUDED.status,
              registry_created_at = COALESCE(
                {self._table_mirror}.registry_created_at,
                EXCLUDED.registry_created_at
              ),
              observed_at = NOW(),
              source_event_id = EXCLUDED.source_event_id
            RETURNING tenant_slug, tenant_uuid, status, observed_at
            """,
            tenant.tenant_slug,
            str(tenant.tenant_id),
            tenant.name,
            tenant.status,
            tenant.created_at,
            str(data.get("correlation_id") or meta.fallback_id) or None,
        )

        if not rows:
            # An upsert that returns no row means the write did not land.
            # Reporting success here would let the consumer commit the offset
            # and lose the tenant permanently.
            raise RuntimeError(
                f"tenant_registry_mirror upsert for slug "
                f"{tenant.tenant_slug!r} returned no row -- refusing to report "
                "the event as projected (the offset would be committed and the "
                "tenant lost from the mirror)"
            )

        logger.info(
            "tenant_registry_mirror: %s -> %s (status=%s)",
            tenant.tenant_slug,
            tenant.tenant_id,
            tenant.status,
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerTenantRegistryProjectionRunner()
    asyncio.run(runner.run())
