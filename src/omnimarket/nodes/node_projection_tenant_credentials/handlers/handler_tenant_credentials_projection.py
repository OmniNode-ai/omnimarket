"""Tenant BYOK credential projection: Kafka -> the ref catalog AND the route.

Consumes the two events the gateway value->ref thin-publisher
(``omnimarket.projection.credential_publisher``) emits:

  * ``credential-registered`` -- INSERTs a new ref row. The event never
    carries the secret value (``ModelCredentialRegisteredEvent`` is
    ``extra="forbid"`` with no value-shaped field), so this projection never
    sees, stores, or could leak it.
  * ``credential-revoked`` -- sets ``revoked_at`` on the matching
    ``api_key_ref``. Never deletes the row (a revoked credential stays in the
    catalog for audit; "revoked" means no longer resolvable by delegation).
    Registered and revoked land on two separate Kafka topics with no
    cross-topic ordering guarantee; a revoke that arrives before its
    matching register persists a tombstone row (OMN-16324) so the later
    register cannot silently un-revoke it -- see ``_project_revoked``.

Only this projection writes ``tenant_inference_credentials`` -- the gateway
handler that publishes these events never touches the database (OMN-15800).

Two read models, one event stream (OMN-17372 blocker b3)
--------------------------------------------------------
Registering a key made it visible; it did not make it USABLE. Executing on a
customer's key requires a row in a different table --
``delegation_routing_tenant_overlay``, the only path that produces
``cost_tier="tenant_byok"`` and threads the tenant's own ``secret_ref``
(``node_delegation_routing_reducer/handlers/handler_delegation_routing.py::
_decision_from_tenant_overlay``). That table had a creating migration
(``node_delegation_routing_reducer/migrations/0001``), a ``GRANT`` to
``tenant_projection_writer``, and NO WRITER OF ANY KIND, so a customer could
register an OpenRouter key and still have no route that selects it.

This projection now derives BOTH read models from the same event, in the same
consumer -- not from a second imperative API call. That placement is the point:
a registration durable in the catalog but absent from the route is exactly the
split being closed, and deriving both from one event makes the two states
unable to disagree. Doing it here rather than in a new node also means it runs
in a process that is already deployed on onex-dev
(``deployment-omnimarket-projection-tenant-credentials-writer.yaml``, bound to
the same ``OMNIDASH_ANALYTICS_DB_URL`` the overlay resolver reads), with the
OMN-16324 cross-topic ordering problem already solved once.

Two invariants govern the overlay half, both fail-CLOSED:

  * **An undeclared provider mints no route.** The provider->backend binding
    comes from ``configs/byok_provider_backends.v1.yaml``; a provider absent
    from it is catalogued and left unrouted. It must never inherit a platform
    backend, because every platform backend carries a HOUSE ``secret_ref``
    (``llm.openrouter.api_key``) and answering a customer on a house
    credential is what OMN-17372 ruling 3 forbids.
  * **Revocation NULLs ``secret_ref``; it does not drop the row.** Dropping it
    would return the tenant to the platform default -- the house ladder --
    which is the same forbidden outcome by a different route. Keeping an
    unresolvable row keeps the tenant on their own backend with no key, which
    fails at the effect boundary instead of quietly succeeding on ours. (The
    typed customer-facing wording of that refusal is OMN-17372 acceptance 2,
    at the effect boundary, not here.)
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
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {"tenant_inference_credentials", "delegation_routing_tenant_overlay"}
)


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

        for required_role in ("credentials", "routing_overlay"):
            if required_role not in _by_role:
                raise ValueError(
                    f"Contract missing required table role {required_role!r}"
                )

        self._table_credentials: str = _by_role["credentials"]
        # OMN-17372: bare/unqualified on purpose. The contract declares this
        # relation's LOGICAL domain as `tenant`, but it lives physically in
        # `public` until OMN-15359 performs the governed per-family copy --
        # omnibase_infra's TENANT_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359
        # enumerates it for exactly that reason, and its sibling
        # `delegation_events` rides the same bridge. Emitting
        # `INSERT INTO "tenant".…` here would address a schema that exists on
        # no lane.
        self._table_routing_overlay: str = _by_role["routing_overlay"]

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
            tenant_id=str(row["tenant_id"]),
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
        # Strictly after the catalog write: the guard below reads the row this
        # statement just produced, so a revoke that landed first (OMN-16324's
        # tombstone) is already visible and blocks the route.
        await self._project_routing_overlay(
            tenant_id=str(tenant_id),
            provider=str(provider),
            api_key_ref=str(api_key_ref),
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _project_routing_overlay(
        self, *, tenant_id: str, provider: str, api_key_ref: str
    ) -> bool:
        """Mint the route that actually selects this customer's key (OMN-17372).

        One row per tenant, keyed ``(tenant_id, BYOK_ALL_TASK_TYPES)``. The
        sentinel task type is what the customer's act actually means -- they
        registered a KEY, not a per-task-class routing policy -- and it keeps
        the write independent of ``task_class_contracts.v1.yaml``, which a
        row-per-task-class fan-out would have to enumerate and would silently
        strand for any class added later.

        Returns ``True`` when a route exists for this credential afterwards,
        ``False`` when none was minted. ``False`` is not an error: an
        undeclared provider is catalogued and left unrouted rather than
        inheriting a platform backend and its house credential.
        """
        backend = resolve_byok_provider_backend(provider)
        if backend is None:
            # Deliberately not a raise and not a DLQ: the credential itself is
            # valid and now visible to its owner. What does not exist is a
            # backend to point it at. Falling back to a platform backend here
            # is the one thing that must never happen (OMN-17372 ruling 3), so
            # the fail-closed outcome is "no row".
            logger.warning(
                "credential-registered for tenant_id=%s names provider=%r, which "
                "is not declared in the BYOK provider catalog "
                "(configs/byok_provider_backends.v1.yaml) -- the credential is "
                "catalogued but NO delegation route was minted for it. A "
                "delegation for this tenant will not resolve this key.",
                tenant_id,
                provider,
            )
            return False

        # The guard is the whole reason this is INSERT ... SELECT ... WHERE
        # NOT EXISTS rather than a plain VALUES upsert. credential-registered
        # and credential-revoked arrive on two topics with no cross-topic
        # ordering guarantee; when the revoke wins the race, _project_revoked
        # has already written a tombstone row carrying revoked_at, and minting
        # a live route from the later register would hand the tenant a working
        # route to a credential they already revoked. ON CONFLICT still covers
        # the ordinary re-delivery case, so the write stays idempotent.
        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_routing_overlay} (
              tenant_id, task_type, backend_id, endpoint_url, model_name,
              secret_ref, timeout_ms, max_tokens, created_at, updated_at
            )
            SELECT
              $1::TEXT, $2::TEXT, $3::TEXT, $4::TEXT, $5::TEXT,
              $6::TEXT, $7::INTEGER, $8::INTEGER, NOW(), NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._table_credentials}
               WHERE api_key_ref = $6::TEXT
                 AND revoked_at IS NOT NULL
            )
            ON CONFLICT (tenant_id, task_type) DO UPDATE SET
              backend_id = EXCLUDED.backend_id,
              endpoint_url = EXCLUDED.endpoint_url,
              model_name = EXCLUDED.model_name,
              secret_ref = EXCLUDED.secret_ref,
              timeout_ms = EXCLUDED.timeout_ms,
              max_tokens = EXCLUDED.max_tokens,
              updated_at = NOW()
            RETURNING tenant_id, task_type, backend_id, secret_ref
            """,
            tenant_id,
            BYOK_ALL_TASK_TYPES,
            backend.backend_id,
            backend.endpoint_url,
            backend.model_name,
            api_key_ref,
            backend.timeout_ms,
            backend.max_tokens,
        )
        if not rows:
            logger.info(
                "credential-registered for tenant_id=%s provider=%s minted no "
                "delegation route: the credential is already revoked, so the "
                "revoke won the cross-topic race and the route stays absent.",
                tenant_id,
                provider,
            )
            return False
        logger.info(
            "BYOK delegation route minted for tenant_id=%s provider=%s "
            "backend_id=%s (task_type=%r) -- delegations for this tenant now "
            "resolve cost_tier tenant_byok on their own credential.",
            tenant_id,
            provider,
            backend.backend_id,
            BYOK_ALL_TASK_TYPES,
        )
        return True

    async def _project_revoked(self, data: dict[str, Any], meta: MessageMeta) -> bool:
        api_key_ref = data.get("api_key_ref")
        tenant_id = data.get("tenant_id")
        if not api_key_ref or not tenant_id:
            logger.warning(
                "credential-revoked missing api_key_ref/tenant_id -- skipping"
            )
            return True

        # OMN-16324: credential-registered and credential-revoked are
        # published to two separate Kafka topics with no cross-topic
        # ordering guarantee (e.g. register-then-immediately-revoke racing
        # different consumer lag on different partitions/topics). A plain
        # "UPDATE ... WHERE revoked_at IS NULL" matches zero rows when the
        # revoke arrives first; the eventual register's own
        # "INSERT ... ON CONFLICT DO UPDATE" would then create the row fresh
        # with revoked_at = NULL, silently un-revoking a credential the
        # customer already revoked.
        #
        # Fix: an UPSERT instead of a bare UPDATE.
        #   * Row already exists (normal order, or a repeat revoke): only
        #     revoked_at changes. COALESCE keeps the earliest revocation
        #     timestamp (idempotent) and this statement never touches
        #     tenant_id/name/provider.
        #   * Row does not exist yet (out-of-order): INSERTs a tombstone row
        #     (name/provider NULL -- not yet known) with revoked_at already
        #     set. _project_registered's own ON CONFLICT DO UPDATE never
        #     touches revoked_at, so when the register event later arrives it
        #     fills in name/provider onto this tombstone without reviving the
        #     credential. created_at on that tombstone records when the
        #     out-of-order revoke was first seen, not the eventual real
        #     registration time -- an acceptable audit-trail tradeoff for a
        #     credential this projection has not been told about yet.
        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_credentials} (
              api_key_ref, tenant_id, name, provider, created_at, revoked_at
            ) VALUES (
              $1, $2, NULL, NULL, NOW(), NOW()
            )
            ON CONFLICT (api_key_ref) DO UPDATE SET
              revoked_at = COALESCE(
                {self._table_credentials}.revoked_at, EXCLUDED.revoked_at
              )
            RETURNING api_key_ref, tenant_id, name, provider, created_at, revoked_at
            """,
            str(api_key_ref),
            str(tenant_id),
        )
        await self._revoke_routing_overlay(
            tenant_id=str(tenant_id), api_key_ref=str(api_key_ref)
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _revoke_routing_overlay(
        self, *, tenant_id: str, api_key_ref: str
    ) -> bool:
        """Un-point this tenant's route from a revoked credential (OMN-17372).

        NULLs ``secret_ref``; keeps the row. Deleting the row is the wrong
        semantic and not merely an unavailable one: with no overlay row,
        ``resolve_tenant_overlay`` returns ``None`` and the tenant falls
        through to the platform default -- the house ladder, on OmniNode's own
        provider credential. That is precisely the outcome OMN-17372 ruling 3
        forbids, so revocation must leave the tenant on their OWN backend with
        no key rather than returning them to ours. (The grant on this table is
        SELECT/INSERT/UPDATE only, so ``DELETE`` is also unavailable; the
        semantic argument is the governing one.)

        Scoped by ``tenant_id`` AND ``secret_ref`` so revoking one credential
        can never blank another tenant's route, and cannot blank this tenant's
        route if they have since re-registered and the row already points at a
        newer ref.

        Re-registration recovers: a fresh key mints a fresh ref, and
        ``_project_routing_overlay``'s ``ON CONFLICT DO UPDATE`` writes it over
        the NULL.
        """
        rows = await self.db.execute(
            f"""
            UPDATE {self._table_routing_overlay}
               SET secret_ref = NULL,
                   updated_at = NOW()
             WHERE tenant_id = $1::TEXT
               AND secret_ref = $2::TEXT
            RETURNING tenant_id, task_type
            """,
            tenant_id,
            api_key_ref,
        )
        if rows:
            logger.info(
                "credential-revoked un-pointed %d BYOK delegation route(s) for "
                "tenant_id=%s -- the row is KEPT with a NULL secret_ref so the "
                "tenant does not fall back to the platform default ladder.",
                len(rows),
                tenant_id,
            )
        return bool(rows)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerTenantCredentialsProjectionRunner()
    asyncio.run(runner.run())
