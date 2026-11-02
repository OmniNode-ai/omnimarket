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
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    resolve_byok_provider_backend,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

from .handler_tenant_credentials_store import (
    registered_expected_version,
    registered_models,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {"tenant_inference_credentials", "delegation_routing_tenant_overlay"}
)


#: The full credential-state row: every catalog column, never a secret.
_CREDENTIAL_STATE_COLUMNS = (
    "api_key_ref, tenant_id, name, provider, created_at, revoked_at, "
    "fingerprint, set_at, expires_at, bound_host, workspace_id, models, version"
)


#: The full route row: everything a replay needs to rebuild the route. The
#: secret_ref is the key's reference, never the key.
_OVERLAY_STATE_COLUMNS = (
    "id, tenant_id, task_type, backend_id, provider, endpoint_url, model_name, "
    "secret_ref, timeout_ms, max_tokens, models, version, created_at, updated_at"
)


def _decoded_state_row(row: dict[str, Any]) -> dict[str, Any]:
    """The row as published: asyncpg hands JSONB back as text, the topic a list."""
    models = row.get("models")
    if isinstance(models, str):
        return {**row, "models": json.loads(models)}
    return row


def _credential_state(route_row: dict[str, Any]) -> dict[str, Any] | None:
    """The key's catalog row the route write returned beside the route.

    ``to_jsonb`` reaches asyncpg as text; a mock database may hand back a dict
    or nothing at all, and nothing means there is no newer row to publish.
    """
    state = route_row.get("credential_state")
    if isinstance(state, str):
        state = json.loads(state)
    return state if isinstance(state, dict) else None


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_timestamp(value: object) -> datetime | None:
    """``set_at`` as asyncpg binds a TIMESTAMPTZ: the event carries ISO text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _registered_plan(data: dict[str, Any]) -> str | None:
    """The plan a credential-registered event carries in ``metadata``, if any.

    OMN-20157. ``metadata`` is a string map the publisher already puts on the
    event, so a plan needs no new wire field and an event published before plans
    existed (no key) resolves the provider's default plan.
    """
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    plan = metadata.get("plan")
    return str(plan) if plan else None


def _registered_model(data: dict[str, Any]) -> str:
    """The model a credential-registered event carries in ``metadata``.

    OMN-20157. The intake resolves the model from the provider's own model list
    with the customer's key and records it beside the plan. An event without one
    (the list could not be read at registration, or the event predates model
    discovery) yields the unresolved marker, which the effect resolves with the
    key before the call. There is no pinned catalogue model to fall back to.
    """
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        model = metadata.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return BYOK_MODEL_UNRESOLVED


class HandlerTenantCredentialsProjectionRunner(BaseProjectionRunner):
    """Projects BYOK credential-registered/-revoked events into tenant_inference_credentials."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        self._standalone_db_tables = tuple(_tables)
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
        # OMN-21217: every bus-backed exposure of a table gets each row this
        # node commits to that table, so each registry's compacted state topic
        # (its reconcile source) carries the same rows its served exposure does.
        self._snapshot_exposures: tuple[ProjectionTableConfig, ...] = tuple(
            exposure for exposure in exposures if exposure.bus_backed
        )
        # A state topic is a bus-backed exposure this node also subscribes to:
        # it reads its own committed rows back, which is what lets a replay
        # rebuild the registry. Topic to table, read from the contract, never
        # named here.
        _subscribed = set(
            self._contract.get("event_bus", {}).get("subscribe_topics", [])
        )
        self._state_tables: dict[str, str] = {
            exposure.topic: exposure.table
            for exposure in self._snapshot_exposures
            if exposure.topic in _subscribed
        }

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
        state_table = self._state_tables.get(topic)
        if state_table == self._table_credentials:
            return await self._apply_credential_state(data)
        if state_table == self._table_routing_overlay:
            return await self._apply_overlay_state(data)
        return False

    async def _publish_snapshot_if_available(
        self,
        row: dict[str, Any] | None,
        meta: MessageMeta,
        data: dict[str, Any],
        *,
        table: str | None = None,
    ) -> None:
        """Best-effort snapshot publish (OMN-15800): a no-op unless this node
        declares a bus_backed exposure of ``table`` AND the write returned a
        real row. ``table`` defaults to the credential catalog."""
        if row is None:
            return
        table = table or self._table_credentials
        source_event_id = str(data.get("correlation_id") or meta.fallback_id)
        for exposure in self._snapshot_exposures:
            if exposure.table != table:
                continue
            await self.publish_snapshot_delta(
                exposure,
                op="upsert",
                row={
                    column: value
                    for column, value in _decoded_state_row(row).items()
                    if column in exposure.columns
                },
                source_event_id=source_event_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
                tenant_id=str(row["tenant_id"]),
            )

    async def _apply_credential_state(self, data: dict[str, Any]) -> bool:
        """Apply one committed credential row read back from the state topic.

        OMN-21217. The row is this node's own earlier commit, so applying it is
        a no-op when the stored row is the same or newer: the upsert writes only
        when the stored version is not above the row's. A revoke is never
        undone by an older-looking row, and nothing is published again, so the
        node cannot feed itself.
        """
        if data.get("op") != "upsert":
            return True
        row = data.get("row")
        if not isinstance(row, dict):
            logger.warning("credential-state message carries no row -- skipping")
            return True
        api_key_ref = row.get("api_key_ref")
        tenant_id = row.get("tenant_id")
        version = row.get("version")
        if not api_key_ref or not tenant_id or not isinstance(version, int):
            logger.warning(
                "credential-state row missing api_key_ref/tenant_id/version -- skipping"
            )
            return True
        created_at = _optional_timestamp(row.get("created_at")) or datetime.now(UTC)
        await self.db_for(self._table_credentials, operation="write").execute(
            f"""
            INSERT INTO {self._table_credentials} (
              api_key_ref, tenant_id, name, provider, created_at, revoked_at,
              fingerprint, set_at, expires_at, bound_host, workspace_id, models,
              version
            ) VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::JSONB, $13
            )
            ON CONFLICT (api_key_ref) DO UPDATE SET
              tenant_id = EXCLUDED.tenant_id,
              name = EXCLUDED.name,
              provider = EXCLUDED.provider,
              revoked_at = COALESCE(
                {self._table_credentials}.revoked_at, EXCLUDED.revoked_at
              ),
              fingerprint = EXCLUDED.fingerprint,
              set_at = EXCLUDED.set_at,
              expires_at = EXCLUDED.expires_at,
              bound_host = EXCLUDED.bound_host,
              workspace_id = EXCLUDED.workspace_id,
              models = EXCLUDED.models,
              version = EXCLUDED.version
            WHERE {self._table_credentials}.version <= EXCLUDED.version
            """,
            str(api_key_ref),
            str(tenant_id),
            _optional_text(row.get("name")),
            _optional_text(row.get("provider")),
            created_at,
            _optional_timestamp(row.get("revoked_at")),
            _optional_text(row.get("fingerprint")),
            _optional_timestamp(row.get("set_at")),
            _optional_timestamp(row.get("expires_at")),
            _optional_text(row.get("bound_host")),
            _optional_text(row.get("workspace_id")),
            None if row.get("models") is None else json.dumps(row["models"]),
            version,
            tenant=str(tenant_id),
        )
        return True

    async def _apply_overlay_state(self, data: dict[str, Any]) -> bool:
        """Apply one committed route row read back from the overlay-state topic.

        OMN-21217, the overlay registry's twin of ``_apply_credential_state``:
        a row is applied only when the stored route's version is not above
        it, and a row that names a key never re-points the route at a key
        already revoked here. Nothing is published again.
        """
        if data.get("op") != "upsert":
            return True
        row = data.get("row")
        if not isinstance(row, dict):
            logger.warning("overlay-state message carries no row -- skipping")
            return True
        tenant_id = row.get("tenant_id")
        task_type = row.get("task_type")
        version = row.get("version")
        if (
            not tenant_id
            or not task_type
            or not row.get("backend_id")
            or not row.get("endpoint_url")
            or not row.get("model_name")
            or not isinstance(version, int)
        ):
            logger.warning(
                "overlay-state row missing tenant_id/task_type/backend/endpoint/"
                "model/version -- skipping"
            )
            return True
        secret_ref = _optional_text(row.get("secret_ref"))
        await self.db_for(self._table_routing_overlay, operation="write").execute(
            f"""
            INSERT INTO {self._table_routing_overlay} (
              tenant_id, task_type, backend_id, provider, endpoint_url, model_name,
              secret_ref, timeout_ms, max_tokens, models, version,
              created_at, updated_at
            )
            SELECT
              $1::TEXT, $2::TEXT, $3::TEXT, $4::TEXT, $5::TEXT, $6::TEXT,
              $7::TEXT, $8::INTEGER, $9::INTEGER, $10::JSONB, $11::BIGINT,
              COALESCE($12::TIMESTAMPTZ, NOW()), COALESCE($13::TIMESTAMPTZ, NOW())
            WHERE $7::TEXT IS NULL OR NOT EXISTS (
              SELECT 1 FROM {self._table_credentials}
               WHERE api_key_ref = $7::TEXT AND revoked_at IS NOT NULL
            )
            ON CONFLICT (tenant_id, task_type) DO UPDATE SET
              backend_id = EXCLUDED.backend_id,
              provider = EXCLUDED.provider,
              endpoint_url = EXCLUDED.endpoint_url,
              model_name = EXCLUDED.model_name,
              secret_ref = EXCLUDED.secret_ref,
              timeout_ms = EXCLUDED.timeout_ms,
              max_tokens = EXCLUDED.max_tokens,
              models = EXCLUDED.models,
              version = EXCLUDED.version,
              updated_at = EXCLUDED.updated_at
            WHERE {self._table_routing_overlay}.version <= EXCLUDED.version
            """,
            str(tenant_id),
            str(task_type),
            str(row["backend_id"]),
            _optional_text(row.get("provider")),
            str(row["endpoint_url"]),
            str(row["model_name"]),
            secret_ref,
            row.get("timeout_ms"),
            row.get("max_tokens"),
            json.dumps(row.get("models") or []),
            version,
            _optional_timestamp(row.get("created_at")),
            _optional_timestamp(row.get("updated_at")),
            tenant=str(tenant_id),
        )
        return True

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

        models = registered_models(data)
        expected_version = registered_expected_version(data)
        rows = await self.db_for(self._table_credentials, operation="write").execute(
            f"""
            INSERT INTO {self._table_credentials} (
              api_key_ref, tenant_id, name, provider, created_at,
              fingerprint, set_at, expires_at, bound_host, workspace_id
            ) VALUES (
              $1, $2, $3, $4, NOW(), $5, $6, $7, $8, $9
            )
            ON CONFLICT (api_key_ref) DO UPDATE SET
              tenant_id = EXCLUDED.tenant_id,
              name = EXCLUDED.name,
              provider = EXCLUDED.provider,
              fingerprint = COALESCE(EXCLUDED.fingerprint, {self._table_credentials}.fingerprint),
              set_at = COALESCE(EXCLUDED.set_at, {self._table_credentials}.set_at),
              expires_at = COALESCE(EXCLUDED.expires_at, {self._table_credentials}.expires_at),
              bound_host = COALESCE(EXCLUDED.bound_host, {self._table_credentials}.bound_host),
              workspace_id = COALESCE(EXCLUDED.workspace_id, {self._table_credentials}.workspace_id),
              version = {self._table_credentials}.version + 1
            RETURNING {_CREDENTIAL_STATE_COLUMNS}
            """,
            str(api_key_ref),
            str(tenant_id),
            str(name),
            str(provider),
            # A locally set key carries both (0005); a hosted event does not,
            # and COALESCE keeps a stored value rather than erasing it.
            _optional_text(data.get("fingerprint")),
            _optional_timestamp(data.get("set_at")),
            # OMN-20991: the key's expiry, so routing can skip a lapsed key.
            _optional_timestamp(data.get("expires_at")),
            # OMN-21012: the lab host whose Claude account owns the key.
            _optional_text(data.get("bound_host")),
            # OMN-20967: the Anthropic workspace the key belongs to.
            _optional_text(data.get("workspace_id")),
            # OMN-15919: bind app.tenant_id to the tenant this statement
            # actually names. The adapter previously derived the GUC on its
            # own from resolve_read_tenant(None) -- a second resolver that
            # has never seen this row -- so the RLS policy compared a value
            # this writer chose against one it did not.
            tenant=str(tenant_id),
        )
        # Strictly after the catalog write: the guard below reads the row this
        # statement just produced, so a revoke that landed first (OMN-16324's
        # tombstone) is already visible and blocks the route.
        route = await self._project_routing_overlay(
            tenant_id=str(tenant_id),
            provider=str(provider),
            api_key_ref=str(api_key_ref),
            plan=_registered_plan(data),
            model=models[0] if models else _registered_model(data),
            models=models,
            expected_version=expected_version,
        )
        # The route write also records the key's own model choices on its
        # catalog row (OMN-21217); that later row is the one to publish.
        chosen = _credential_state(route) if route is not None else None
        committed = chosen if chosen is not None else (rows[0] if rows else None)
        await self._publish_snapshot_if_available(committed, meta, data)
        await self._publish_snapshot_if_available(
            route, meta, data, table=self._table_routing_overlay
        )
        return True

    async def _project_routing_overlay(
        self,
        *,
        tenant_id: str,
        provider: str,
        api_key_ref: str,
        plan: str | None = None,
        model: str = BYOK_MODEL_UNRESOLVED,
        models: list[str] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Mint the route that actually selects this customer's key (OMN-17372).

        One row per tenant, keyed ``(tenant_id, BYOK_ALL_TASK_TYPES)``. The
        sentinel task type is what the customer's act actually means -- they
        registered a KEY, not a per-task-class routing policy -- and it keeps
        the write independent of ``task_class_contracts.v1.yaml``, which a
        row-per-task-class fan-out would have to enumerate and would silently
        strand for any class added later.

        Returns the committed route row, carrying the key's catalog row as the
        route write left it under ``credential_state``, when a route exists
        for this credential afterwards, ``None`` when none was minted.
        ``None`` is not an error: an undeclared provider is catalogued and left
        unrouted rather than inheriting a platform backend and its house
        credential.

        OMN-21217: the same statement records the chosen models on the key's
        own catalog row and raises its version, only when the route write
        applied. Routing binds a pooled key to its own choices, so a refused
        version-conflict replace leaves both the route and the key unchanged.
        """
        # OMN-20157: the plan the credential was registered under selects which
        # product endpoint the route addresses; the overlay row records it
        # through the ``backend_id`` it is minted with. No plan means the
        # provider's default plan (glm: the general API, byok-glm-general). A
        # detection-only plan (glm coding_plan) resolves to nothing here, so no
        # route to the Coding Plan endpoint is ever minted for a customer.
        backend = resolve_byok_provider_backend(provider, plan=plan)
        if backend is None:
            # Deliberately not a raise and not a DLQ: the credential itself is
            # valid and now visible to its owner. What does not exist is a
            # backend to point it at. Falling back to a platform backend here
            # is the one thing that must never happen (OMN-17372 ruling 3), so
            # the fail-closed outcome is "no row".
            logger.warning(
                "credential-registered for tenant_id=%s names provider=%r, which "
                "is not declared in the BYOK provider catalog "
                "(configs/byok_provider_backends.v1.yaml) for plan=%r, or whose "
                "plan is declared detection-only (customer_routable false: the "
                "provider's terms bar it from third-party systems, OMN-20157) -- "
                "the credential is catalogued but NO delegation route was minted "
                "for it. A delegation for this tenant will not resolve this key.",
                tenant_id,
                provider,
                plan,
            )
            return None

        # Runs after the route write in one statement, and only when it wrote:
        # the key's own choices change exactly when the route's do.
        chosen_models = f"""
            , chosen AS (
              UPDATE {self._table_credentials}
              SET models = NULLIF($10::JSONB, '[]'::JSONB),
                  version = {self._table_credentials}.version + 1
              WHERE api_key_ref = $7::TEXT AND tenant_id = $1::TEXT
                AND revoked_at IS NULL
                AND EXISTS (SELECT 1 FROM route)
              RETURNING {_CREDENTIAL_STATE_COLUMNS}
            )
            SELECT route.*, (SELECT to_jsonb(chosen) FROM chosen) AS credential_state
            FROM route
            """

        if expected_version is not None:
            rows = await self.db_for(
                self._table_routing_overlay, operation="write"
            ).execute(
                f"""
                WITH route AS (
                UPDATE {self._table_routing_overlay}
                SET backend_id = $3, provider = $4, endpoint_url = $5,
                    model_name = $6, secret_ref = $7, timeout_ms = $8,
                    max_tokens = $9, models = $10::JSONB, version = version + 1,
                    updated_at = NOW()
                WHERE tenant_id = $1 AND task_type = $2 AND version = $11
                  AND NOT EXISTS (SELECT 1 FROM {self._table_credentials}
                      WHERE api_key_ref = $7 AND revoked_at IS NOT NULL)
                RETURNING {_OVERLAY_STATE_COLUMNS}
                )
                {chosen_models}
                """,
                tenant_id,
                BYOK_ALL_TASK_TYPES,
                backend.backend_id,
                backend.provider,
                backend.endpoint_url,
                model,
                api_key_ref,
                backend.timeout_ms,
                backend.max_tokens,
                json.dumps(models or []),
                expected_version,
                tenant=tenant_id,
            )
            if not rows:
                current = await self.db_for(
                    self._table_routing_overlay, operation="write"
                ).execute(
                    f"SELECT version FROM {self._table_routing_overlay} WHERE tenant_id = $1 AND task_type = $2",
                    tenant_id,
                    BYOK_ALL_TASK_TYPES,
                    tenant=tenant_id,
                )
                logger.warning(
                    "credential_models_version_conflict tenant_id=%s current_version=%s",
                    tenant_id,
                    current[0]["version"] if current else None,
                )
                return None
            return dict(rows[0])

        # The guard is the whole reason this is INSERT ... SELECT ... WHERE
        # NOT EXISTS rather than a plain VALUES upsert. credential-registered
        # and credential-revoked arrive on two topics with no cross-topic
        # ordering guarantee; when the revoke wins the race, _project_revoked
        # has already written a tombstone row carrying revoked_at, and minting
        # a live route from the later register would hand the tenant a working
        # route to a credential they already revoked. ON CONFLICT still covers
        # the ordinary re-delivery case, so the write stays idempotent.
        rows = await self.db_for(
            self._table_routing_overlay, operation="write"
        ).execute(
            f"""
            WITH route AS (
            INSERT INTO {self._table_routing_overlay} (
              tenant_id, task_type, backend_id, provider, endpoint_url, model_name,
              secret_ref, timeout_ms, max_tokens, created_at, updated_at, models, version
            )
            SELECT
              $1::TEXT, $2::TEXT, $3::TEXT, $4::TEXT, $5::TEXT, $6::TEXT,
              $7::TEXT, $8::INTEGER, $9::INTEGER, NOW(), NOW(), $10::JSONB, 1
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._table_credentials}
               WHERE api_key_ref = $7::TEXT
                 AND revoked_at IS NOT NULL
            )
            ON CONFLICT (tenant_id, task_type) DO UPDATE SET
              backend_id = EXCLUDED.backend_id,
              provider = EXCLUDED.provider,
              endpoint_url = EXCLUDED.endpoint_url,
              model_name = EXCLUDED.model_name,
              secret_ref = EXCLUDED.secret_ref,
              timeout_ms = EXCLUDED.timeout_ms,
              max_tokens = EXCLUDED.max_tokens,
              models = EXCLUDED.models,
              version = {self._table_routing_overlay}.version + 1,
              updated_at = NOW()
            WHERE NOT EXISTS (SELECT 1 FROM {self._table_credentials}
                WHERE api_key_ref = $7::TEXT AND revoked_at IS NOT NULL)
            RETURNING {_OVERLAY_STATE_COLUMNS}
            )
            {chosen_models}
            """,
            tenant_id,
            BYOK_ALL_TASK_TYPES,
            backend.backend_id,
            backend.provider,
            backend.endpoint_url,
            model,
            api_key_ref,
            backend.timeout_ms,
            backend.max_tokens,
            json.dumps(models or []),
            # OMN-15919: bind app.tenant_id to the tenant this statement
            # actually names. The adapter previously derived the GUC on its
            # own from resolve_read_tenant(None) -- a second resolver that
            # has never seen this row -- so the RLS policy compared a value
            # this writer chose against one it did not.
            tenant=str(tenant_id),
        )
        if not rows:
            logger.info(
                "credential-registered for tenant_id=%s provider=%s minted no "
                "delegation route: the credential is already revoked, so the "
                "revoke won the cross-topic race and the route stays absent.",
                tenant_id,
                provider,
            )
            return None
        logger.info(
            "BYOK delegation route minted for tenant_id=%s provider=%s "
            "backend_id=%s (task_type=%r) -- delegations for this tenant now "
            "resolve cost_tier tenant_byok on their own credential.",
            tenant_id,
            provider,
            backend.backend_id,
            BYOK_ALL_TASK_TYPES,
        )
        return dict(rows[0])

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
        rows = await self.db_for(self._table_credentials, operation="write").execute(
            f"""
            INSERT INTO {self._table_credentials} (
              api_key_ref, tenant_id, name, provider, created_at, revoked_at
            ) VALUES (
              $1, $2, NULL, NULL, NOW(), NOW()
            )
            ON CONFLICT (api_key_ref) DO UPDATE SET
              revoked_at = COALESCE(
                {self._table_credentials}.revoked_at, EXCLUDED.revoked_at
              ),
              version = {self._table_credentials}.version + 1
            RETURNING {_CREDENTIAL_STATE_COLUMNS}
            """,
            str(api_key_ref),
            str(tenant_id),
            # OMN-15919: bind app.tenant_id to the tenant this statement
            # actually names. The adapter previously derived the GUC on its
            # own from resolve_read_tenant(None) -- a second resolver that
            # has never seen this row -- so the RLS policy compared a value
            # this writer chose against one it did not.
            tenant=str(tenant_id),
        )
        routes = await self._revoke_routing_overlay(
            tenant_id=str(tenant_id), api_key_ref=str(api_key_ref)
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        for route in routes:
            await self._publish_snapshot_if_available(
                route, meta, data, table=self._table_routing_overlay
            )
        return True

    async def _revoke_routing_overlay(
        self, *, tenant_id: str, api_key_ref: str
    ) -> list[dict[str, Any]]:
        """Un-point this tenant's route from a revoked credential (OMN-17372).

        NULLs ``secret_ref``; keeps the row. This is a TOMBSTONE, and the
        reducer reads it as an absent credential -- not as a route.

        OMN-18191 corrected the reasoning this method used to carry, which had
        gone stale and was actively misleading. The old text argued that
        deleting the row was the wrong semantic because a tenant with no
        overlay row "falls through to the platform default -- the house
        ladder, on OmniNode's own provider credential", the outcome OMN-17372
        ruling 3 forbids. That has not been true since OMN-17082: a
        customer-attributed tenant with no overlay row is REFUSED by
        ``refuse_keyless_customer_on_cloud`` before tier iteration, so the
        ladder is unreachable for them either way. Keep-the-row survives on
        its remaining merits -- the row records which backend the tenant was
        pointed at, and the grant on this table is SELECT/INSERT/UPDATE only
        so ``DELETE`` would need a migration -- not on the obsolete one.

        What the tombstone is NOT is a state the reducer may treat as
        routable. Until OMN-18191 it did: the blanked row still looked like a
        route, the terminus was told an absent ref meant the customer's own
        auth-free endpoint, and the delegation reached the vendor with no
        credential attached. The reducer now refuses any overlay row whose
        ``secret_ref`` is missing, empty or whitespace on the cloud surface
        (``handler_delegation_routing.delta``), which is what makes a
        withdrawn tenant and a never-keyed tenant one state rather than two
        similar ones. That guard keys on the ABSENCE of a usable ref, not on
        this writer, so a future path that leaves a partial row is covered
        without this method having to be the one that remembers.

        Scoped by ``tenant_id`` AND ``secret_ref`` so revoking one credential
        can never blank another tenant's route, and cannot blank this tenant's
        route if they have since re-registered and the row already points at a
        newer ref.

        Re-registration recovers: a fresh key mints a fresh ref, and
        ``_project_routing_overlay``'s ``ON CONFLICT DO UPDATE`` writes it over
        the NULL.
        """
        rows = await self.db_for(
            self._table_routing_overlay, operation="write"
        ).execute(
            f"""
            UPDATE {self._table_routing_overlay}
               SET secret_ref = NULL, version = version + 1,
                   updated_at = NOW()
             WHERE tenant_id = $1::TEXT
               AND secret_ref = $2::TEXT
            RETURNING {_OVERLAY_STATE_COLUMNS}
            """,
            tenant_id,
            api_key_ref,
            # OMN-15919: bind app.tenant_id to the tenant this statement
            # actually names. The adapter previously derived the GUC on its
            # own from resolve_read_tenant(None) -- a second resolver that
            # has never seen this row -- so the RLS policy compared a value
            # this writer chose against one it did not.
            tenant=str(tenant_id),
        )
        if rows:
            logger.info(
                "credential-revoked un-pointed %d BYOK delegation route(s) for "
                "tenant_id=%s -- the row is KEPT with a NULL secret_ref so the "
                "tenant does not fall back to the platform default ladder.",
                len(rows),
                tenant_id,
            )
        # The blanked routes, each to be published as it now stands (OMN-21217).
        return [dict(row) for row in rows or []]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerTenantCredentialsProjectionRunner()
    asyncio.run(runner.run())
