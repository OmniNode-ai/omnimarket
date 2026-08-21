"""Tenant inference-credential projection: Kafka -> tenant_inference_credentials (OMN-16316).

Consumes the two events the gateway value->ref thin-publisher
(``omnimarket.projection.credential_publisher``) emits:

  * ``credential-registered`` -- INSERTs a new ref row. The event never
    carries the secret value (``ModelCredentialRegisteredEvent`` is
    ``extra="forbid"`` with no value-shaped field), so this projection never
    sees, stores, or could leak it.
  * ``credential-revoked`` -- sets ``revoked_at`` on the matching
    ``api_key_ref``. Never deletes the row (a revoked credential stays in the
    catalog for audit; "revoked" means no longer resolvable by delegation).

Only this projection writes ``tenant_inference_credentials`` -- the gateway
handler that publishes these events never touches the database (OMN-15800).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset({"tenant_inference_credentials"})


class HandlerTenantCredentialsProjectionRunner(BaseProjectionRunner):
    """Projects BYOK credential-registered/-revoked events into tenant_inference_credentials."""

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

        if "credentials" not in _by_role:
            raise ValueError("Contract missing required table role 'credentials'")

        self._table_credentials: str = _by_role["credentials"]

        node_name = str(self._contract.get("name", "projection_tenant_credentials"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal def-B handler entrypoint. Delegates to project_event().

        ``request`` is a magic single-positional-param name the shared
        ``runtime_local_adapter`` recognizes and adapts (OMN-14355 canonical
        handler shape) -- unlike the prior ``input_data`` name, which the
        canon-shape ratchet classified as a nonadaptable, non-canonical
        signature.
        """
        topics = self.subscribe_topics
        topic = str(request.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(request.pop("_partition", 0)),
            offset=int(request.pop("_offset", 0)),
            fallback_id=str(request.pop("_fallback_id", "")),
            topic=topic,
        )
        ok = asyncio.run(self.project_event(topic, request, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        subscribe = self.subscribe_topics
        topic_registered = subscribe[0] if len(subscribe) > 0 else ""
        topic_revoked = subscribe[1] if len(subscribe) > 1 else ""

        if topic == topic_registered:
            return await self._project_registered(data, meta)
        if topic == topic_revoked:
            return await self._project_revoked(data, meta)
        return False

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish (OMN-15800): a no-op unless this node
        declares a bus_backed exposure AND the write returned a real row."""
        if self._snapshot_exposure is None or row is None:
            return
        source_event_id = str(data.get("correlation_id") or meta.fallback_id)
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )

    async def _project_registered(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        api_key_ref = data.get("api_key_ref")
        tenant_id = data.get("tenant_id")
        provider = data.get("provider")
        name = data.get("name")
        if not api_key_ref or not tenant_id or not provider or not name:
            logger.warning(
                "credential-registered missing api_key_ref/tenant_id/provider/name "
                "-- skipping"
            )
            return True

        # Defense in depth: the publisher's event model is extra="forbid" with
        # no value-shaped field, so this can never legitimately be present --
        # but if a malformed/forged message ever carries one, refuse to
        # persist it rather than silently dropping just that key.
        for leak_key in ("value", "key_value", "secret", "api_key"):
            if leak_key in data:
                raise ValueError(
                    f"credential-registered payload carries a secret-shaped "
                    f"field {leak_key!r} -- refusing to project (would risk "
                    "persisting a secret value into the ref catalog)"
                )

        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_credentials} (
              api_key_ref, tenant_id, name, provider, created_at
            ) VALUES (
              $1, $2, $3, $4, NOW()
            )
            ON CONFLICT (api_key_ref) DO UPDATE SET
              tenant_id = EXCLUDED.tenant_id,
              name = EXCLUDED.name,
              provider = EXCLUDED.provider
            RETURNING api_key_ref, tenant_id, name, provider, created_at, revoked_at
            """,
            str(api_key_ref),
            str(tenant_id),
            str(name),
            str(provider),
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _project_revoked(self, data: dict[str, Any], meta: MessageMeta) -> bool:
        api_key_ref = data.get("api_key_ref")
        if not api_key_ref:
            logger.warning("credential-revoked missing api_key_ref -- skipping")
            return True

        rows = await self.db.execute(
            f"""
            UPDATE {self._table_credentials}
            SET revoked_at = NOW()
            WHERE api_key_ref = $1 AND revoked_at IS NULL
            RETURNING api_key_ref, tenant_id, name, provider, created_at, revoked_at
            """,
            str(api_key_ref),
        )
        # A revoke for a ref this projection never inserted (or already
        # revoked) matches zero rows -- a no-op, not an error: revocation is
        # idempotent and the gateway never reads this table before publishing.
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerTenantCredentialsProjectionRunner()
    asyncio.run(runner.run())
