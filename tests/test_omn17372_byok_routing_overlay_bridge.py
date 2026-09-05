# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The BYOK registration -> routing-overlay bridge (OMN-17372 blocker b3).

The gap these tests close, stated as it was found: registering a customer key
wrote ``tenant_inference_credentials``, but EXECUTING on that key requires a
row in ``delegation_routing_tenant_overlay`` -- the only path that produces
``cost_tier="tenant_byok"`` -- and that table had a creating migration, a
GRANT, and no writer of any kind, repo-wide. A customer could register an
OpenRouter key and still have no route that selects it.

``TestRoutingResolvesTenantByok`` is the load-bearing one: it drives the row
this projection writes through the REAL resolver and the REAL routing reducer
rather than asserting on SQL text, so it fails if either end of the bridge
moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.routing.byok_provider_backends import (
    BYOK_CATALOG_SCHEMA_VERSION,
    CATALOG_PATH,
    ByokCatalogError,
    load_byok_provider_catalog,
    resolve_byok_provider_backend,
)
from omnimarket.routing.tenant_overlay_resolver import (
    BYOK_ALL_TASK_TYPES,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"

TENANT = "acme-corp"
PROVIDER = "openrouter"
API_KEY_REF = f"cred_{TENANT}_{PROVIDER}_0123456789abcdef"

BIFROST_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


def _make_meta() -> MessageMeta:
    return MessageMeta(partition=0, offset=0, fallback_id="fallback-omn17372")


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.execute_many = AsyncMock()
    db.execute_in_transaction = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    db.connect = AsyncMock()
    db.close = AsyncMock()
    return db


@pytest.fixture
def runner(mock_db: AsyncMock) -> HandlerTenantCredentialsProjectionRunner:
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = mock_db
    return r


def _registered_event(provider: str = PROVIDER) -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "provider": provider,
        "name": "my-openrouter-key",
        "api_key_ref": API_KEY_REF,
        "metadata": {},
    }


def _overlay_calls(mock_db: AsyncMock) -> list[Any]:
    """Every ``db.execute`` call whose SQL targets the overlay table."""
    return [
        call
        for call in mock_db.execute.await_args_list
        if TENANT_OVERLAY_TABLE in str(call.args[0])
    ]


class TestCatalog:
    def test_openrouter_is_declared_as_the_minimum_bar(self) -> None:
        backend = resolve_byok_provider_backend("openrouter")
        assert backend is not None
        assert backend.endpoint_url.startswith("https://openrouter.ai/")

    def test_provider_match_is_case_insensitive_and_trimmed(self) -> None:
        assert resolve_byok_provider_backend("  OpenRouter ") == (
            resolve_byok_provider_backend("openrouter")
        )

    def test_undeclared_provider_resolves_to_none_not_a_platform_backend(self) -> None:
        # OMN-17373: openai has no delegation backend yet. Fail-closed means
        # None, never "the nearest platform rung".
        assert resolve_byok_provider_backend("openai") is None
        assert resolve_byok_provider_backend("anthropic") is None

    def test_no_declared_backend_carries_a_secret_ref_field(self) -> None:
        """The house-credential inheritance path is absent by construction.

        Every platform backend in bifrost_delegation.yaml carries a HOUSE
        ``secret_ref``. If this catalog's model ever grows one, a customer's
        route could be minted pointing at a house key.
        """
        from omnimarket.routing.byok_provider_backends import ModelByokProviderBackend

        assert "secret_ref" not in ModelByokProviderBackend.model_fields

    def test_catalog_refuses_a_wrong_schema_version(self, tmp_path: Path) -> None:
        from omnimarket.routing import byok_provider_backends as mod

        bad = tmp_path / "byok.yaml"
        bad.write_text(
            yaml.safe_dump({"schema_version": "nope.v9", "providers": []}),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="schema_version"):
            mod._read_catalog(bad)

    def test_catalog_refuses_a_duplicate_provider(self, tmp_path: Path) -> None:
        from omnimarket.routing import byok_provider_backends as mod

        entry = {
            "provider": "openrouter",
            "backend_id": "byok-openrouter",
            "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            "model_name": "m",
        }
        dupe = tmp_path / "byok.yaml"
        dupe.write_text(
            yaml.safe_dump(
                {
                    "schema_version": BYOK_CATALOG_SCHEMA_VERSION,
                    "providers": [entry, dict(entry)],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="more than once"):
            mod._read_catalog(dupe)


class TestPlatformContractParity:
    """The catalog duplicates two values from bifrost_delegation.yaml; gate the drift.

    The projection writer deliberately does not import the bifrost loader (it
    deep-merges a site overlay and can raise at load time), so the duplication
    is real. This test is the mechanism that keeps it honest: repoint the
    platform's OpenRouter rung and the BYOK catalog must move with it.
    """

    @staticmethod
    def _platform_backends() -> list[dict[str, Any]]:
        payload = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
        return [b for b in payload.get("backends", []) if isinstance(b, dict)]

    def test_every_byok_binding_matches_a_live_platform_backend(self) -> None:
        platform = self._platform_backends()
        assert platform, "bifrost_delegation.yaml declared no backends"
        for backend in load_byok_provider_catalog().values():
            matches = [
                b
                for b in platform
                if b.get("endpoint_url") == backend.endpoint_url
                and b.get("model_name") == backend.model_name
            ]
            assert matches, (
                f"BYOK provider {backend.provider!r} declares endpoint_url="
                f"{backend.endpoint_url!r} model_name={backend.model_name!r}, "
                "which matches no backend in bifrost_delegation.yaml. Either the "
                "platform rung was repointed and "
                f"{CATALOG_PATH.name} was not, or this binding addresses a model "
                "nothing has ever probed."
            )

    def test_byok_backend_ids_never_collide_with_platform_rung_names(self) -> None:
        platform_ids = {b.get("backend_id") for b in self._platform_backends()}
        for backend in load_byok_provider_catalog().values():
            assert backend.backend_id not in platform_ids, (
                f"BYOK backend_id {backend.backend_id!r} collides with a platform "
                "rung; cost and tier accounting would attribute a "
                "customer-paid call to a house rung."
            )


class TestOverlayWrite:
    @pytest.mark.asyncio
    async def test_credential_registered_upserts_the_routing_overlay_row(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        assert await runner.project_event(
            TOPIC_REGISTERED, _registered_event(), _make_meta()
        )

        calls = _overlay_calls(mock_db)
        assert len(calls) == 1, (
            "registering a BYOK key must mint exactly one delegation-routing "
            "overlay row; before OMN-17372 it minted none and the key was "
            "unusable"
        )
        sql, *params = calls[0].args
        backend = resolve_byok_provider_backend(PROVIDER)
        assert backend is not None
        assert params == [
            TENANT,
            BYOK_ALL_TASK_TYPES,
            backend.backend_id,
            backend.endpoint_url,
            backend.model_name,
            API_KEY_REF,
            backend.timeout_ms,
            backend.max_tokens,
        ]
        # The tenant's OWN ref, never a house ref.
        assert "llm.openrouter.api_key" not in str(sql)

    @pytest.mark.asyncio
    async def test_overlay_write_is_idempotent_on_redelivery(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """Kafka redelivery must converge, not accumulate rows."""
        await runner.project_event(TOPIC_REGISTERED, _registered_event(), _make_meta())
        sql = str(_overlay_calls(mock_db)[0].args[0])
        assert "ON CONFLICT (tenant_id, task_type) DO UPDATE" in sql

    @pytest.mark.asyncio
    async def test_overlay_write_is_tenant_scoped(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await runner.project_event(TOPIC_REGISTERED, _registered_event(), _make_meta())
        params = list(_overlay_calls(mock_db)[0].args[1:])
        assert params[0] == TENANT
        # The ref itself is tenant-namespaced by mint_api_key_ref, so a row can
        # never point at another tenant's secret.
        assert params[5].startswith(f"cred_{TENANT}_")

    @pytest.mark.asyncio
    async def test_undeclared_provider_mints_no_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """Ruling 3: no customer-reachable house-credential path.

        An undeclared provider must be catalogued and left UNROUTED. Inheriting
        the nearest platform backend would hand the customer a working answer
        paid for on OmniNode's credential.
        """
        assert await runner.project_event(
            TOPIC_REGISTERED, _registered_event(provider="openai"), _make_meta()
        )

        assert _overlay_calls(mock_db) == []
        # ...but the credential is still catalogued, so the customer sees it.
        assert any(
            "tenant_inference_credentials" in str(call.args[0])
            for call in mock_db.execute.await_args_list
        )

    @pytest.mark.asyncio
    async def test_register_after_an_out_of_order_revoke_mints_no_live_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """OMN-16324's race, applied to the route rather than the catalog.

        The two events land on separate topics with no cross-topic ordering
        guarantee. When the revoke wins, the catalog holds a tombstone with
        ``revoked_at`` set; the later register must not mint a live route to a
        credential the customer already revoked.
        """
        await runner.project_event(TOPIC_REGISTERED, _registered_event(), _make_meta())
        sql = str(_overlay_calls(mock_db)[0].args[0])
        assert "WHERE NOT EXISTS" in sql
        assert "revoked_at IS NOT NULL" in sql


class TestRevocation:
    @pytest.mark.asyncio
    async def test_revocation_nulls_the_overlay_secret_ref_and_keeps_the_row(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """Dropping the row would return the tenant to the HOUSE ladder.

        With no overlay row, ``resolve_tenant_overlay`` returns None and
        routing falls through to the platform default -- which the contract
        itself calls "fail-open, not fail-closed" and which runs on OmniNode's
        provider credentials. Revocation therefore un-points the route instead
        of removing it.
        """
        assert await runner.project_event(
            TOPIC_REVOKED,
            {"tenant_id": TENANT, "api_key_ref": API_KEY_REF},
            _make_meta(),
        )

        calls = _overlay_calls(mock_db)
        assert len(calls) == 1
        sql, *params = calls[0].args
        assert "UPDATE" in str(sql)
        assert "SET secret_ref = NULL" in " ".join(str(sql).split())
        assert "DELETE" not in str(sql).upper()
        assert params == [TENANT, API_KEY_REF]

    @pytest.mark.asyncio
    async def test_revocation_is_scoped_to_the_revoked_ref_of_that_tenant(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await runner.project_event(
            TOPIC_REVOKED,
            {"tenant_id": TENANT, "api_key_ref": API_KEY_REF},
            _make_meta(),
        )
        sql = " ".join(str(_overlay_calls(mock_db)[0].args[0]).split())
        assert "WHERE tenant_id = $1::TEXT AND secret_ref = $2::TEXT" in sql


class _FakeOverlayReader:
    """Stands in for ``PostgresReadDatabaseAdapter`` over the overlay table."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.queries: list[dict[str, object] | None] = []

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        assert table == TENANT_OVERLAY_TABLE
        self.queries.append(filters)
        if filters is None:
            return list(self._rows)
        return [
            row
            for row in self._rows
            if all(row.get(key) == value for key, value in filters.items())
        ]


def _row_from_registration() -> dict[str, object]:
    """The row shape ``_project_routing_overlay`` writes, as Postgres returns it."""
    backend = resolve_byok_provider_backend(PROVIDER)
    assert backend is not None
    return {
        "tenant_id": TENANT,
        "task_type": BYOK_ALL_TASK_TYPES,
        "backend_id": backend.backend_id,
        "endpoint_url": backend.endpoint_url,
        "model_name": backend.model_name,
        "secret_ref": API_KEY_REF,
        "timeout_ms": backend.timeout_ms,
        "max_tokens": backend.max_tokens,
    }


class TestSentinelResolution:
    def test_sentinel_row_resolves_for_a_task_type_it_does_not_name(self) -> None:
        db = _FakeOverlayReader([_row_from_registration()])

        overlay = resolve_tenant_overlay(
            db, tenant_id=TENANT, task_type="code_generation"
        )

        assert overlay is not None
        # Stamped with the REQUESTED task type: delta() asserts equality with
        # the request's own task_type and would raise on the raw sentinel.
        assert overlay.task_type == "code_generation"
        assert overlay.secret_ref == API_KEY_REF

    def test_exact_row_wins_over_the_sentinel(self) -> None:
        exact = dict(_row_from_registration())
        exact["task_type"] = "code_generation"
        exact["backend_id"] = "tenant-pinned-backend"
        db = _FakeOverlayReader([_row_from_registration(), exact])

        overlay = resolve_tenant_overlay(
            db, tenant_id=TENANT, task_type="code_generation"
        )

        assert overlay is not None
        assert overlay.backend_id == "tenant-pinned-backend"
        # Narrower-wins is proven by the reader never being asked for the
        # sentinel at all.
        assert db.queries == [{"tenant_id": TENANT, "task_type": "code_generation"}]

    def test_another_tenants_sentinel_row_is_never_resolved(self) -> None:
        other = dict(_row_from_registration())
        other["tenant_id"] = "someone-else"
        db = _FakeOverlayReader([other])

        assert resolve_tenant_overlay(db, tenant_id=TENANT, task_type="review") is None

    def test_no_row_still_resolves_to_none(self) -> None:
        db = _FakeOverlayReader([])
        assert resolve_tenant_overlay(db, tenant_id=TENANT, task_type="review") is None


class TestRoutingResolvesTenantByok:
    """The bridge, end to end, over the real reducer.

    This is the assertion the ticket is written around: a registered key
    produces a delegation that resolves ``cost_tier="tenant_byok"`` on the
    customer's own ref.
    """

    def test_registered_key_routes_a_delegation_at_cost_tier_tenant_byok(
        self,
    ) -> None:
        from omnimarket.nodes.node_delegation_orchestrator.models import (
            ModelDelegationRequest,
        )
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        db = _FakeOverlayReader([_row_from_registration()])
        overlay = resolve_tenant_overlay(
            db, tenant_id=TENANT, task_type="code_generation"
        )
        assert overlay is not None

        request = ModelDelegationRequest(
            correlation_id=uuid4(),
            prompt="write a function that returns 4",
            task_type="code_generation",
            emitted_at=datetime.now(tz=UTC),
            tenant_id=TENANT,
        )
        decision = delta(request, tenant_overlay=overlay)

        assert decision.cost_tier == "tenant_byok"
        assert decision.api_key_ref == API_KEY_REF
        assert decision.endpoint_url.startswith("https://openrouter.ai/")
        assert decision.tier_name == "tenant_overlay"

    def test_a_tenant_with_no_row_does_not_reach_tenant_byok(self) -> None:
        """The negative half: absent bridge => no BYOK route, by the same path."""
        db = _FakeOverlayReader([])
        assert (
            resolve_tenant_overlay(db, tenant_id=TENANT, task_type="code_generation")
            is None
        )

    def test_a_revoked_credential_yields_a_route_with_no_key(self) -> None:
        """Post-revocation the route survives but carries no credential.

        Proven here so the property cannot be lost silently: the tenant stays
        on their OWN backend, unresolvable, rather than being handed back to
        the platform's house ladder.
        """
        from omnimarket.nodes.node_delegation_orchestrator.models import (
            ModelDelegationRequest,
        )
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        revoked = dict(_row_from_registration())
        revoked["secret_ref"] = None
        db = _FakeOverlayReader([revoked])
        overlay = resolve_tenant_overlay(
            db, tenant_id=TENANT, task_type="code_generation"
        )
        assert overlay is not None

        decision = delta(
            ModelDelegationRequest(
                correlation_id=uuid4(),
                prompt="hello",
                task_type="code_generation",
                emitted_at=datetime.now(tz=UTC),
                tenant_id=TENANT,
            ),
            tenant_overlay=overlay,
        )

        assert decision.cost_tier == "tenant_byok"
        assert decision.api_key_ref is None


class TestContractShape:
    def test_contract_declares_the_overlay_table_as_a_write_target(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_tenant_credentials"
            / "contract.yaml"
        )
        payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        by_name = {t["name"]: t for t in payload["db_io"]["db_tables"]}
        assert TENANT_OVERLAY_TABLE in by_name, (
            "the overlay write must be declared, not incidental -- an undeclared "
            "write is invisible to the relation-ownership inventory"
        )
        assert by_name[TENANT_OVERLAY_TABLE]["access"] == "write"
        assert by_name[TENANT_OVERLAY_TABLE]["role"] == "routing_overlay"

    def test_handler_refuses_a_contract_missing_the_overlay_role(
        self, tmp_path: Path
    ) -> None:
        """A dropped declaration must fail loudly at construction.

        Without this the node would start, consume, and write only half the
        state -- which is the exact shape of the defect being fixed.
        """
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_tenant_credentials"
            / "contract.yaml"
        )
        payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        payload["db_io"]["db_tables"] = [
            t
            for t in payload["db_io"]["db_tables"]
            if t["name"] != TENANT_OVERLAY_TABLE
        ]
        stripped = tmp_path / "contract.yaml"
        stripped.write_text(yaml.safe_dump(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="routing_overlay"):
            HandlerTenantCredentialsProjectionRunner(contract_path=stripped)
