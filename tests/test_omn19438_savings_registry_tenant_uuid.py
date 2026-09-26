# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19438: savings rows carry the registry tenant UUID, never the house slug.

Measured on the .201 dev lane (``omnidash_analytics.savings_estimates``,
2026-09-24): 5,582 rows under ``tenant_id = 'omninode'`` against 99 under the
house UUID, the newest slug row written minutes before this change. Every lane
runtime reads ``ONEX_TENANT_ID`` as empty, so every savings event without a
producer-recorded tenant fell to ``house_tenant_write_stamp``, which answers in
the representation of the table's column -- and ``savings_estimates.tenant_id``
is TEXT, so it answered with the SLUG. The reader
(``GET /v1/tenants/me/savings``) binds the UUID, so none of those rows is
visible to the tenant they belong to.

The fix resolves the house identity through the SAME registry seam that already
resolves every producer-recorded identity (``tenant_registry_mirror``, then the
closed legacy map), so the one authoritative form -- the UUID -- is what lands.
When no UUID resolves, the write is refused with a typed error before any SQL.

AC2: the two other projection defaults of the same shape
(``handler_projection_delegation.DEFAULT_TENANT`` and
``model_inference_response_projection.DEFAULT_TENANT``) resolve to the UUID too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers import (
    handler_projection_delegation,
)
from omnimarket.nodes.node_projection_delegation_inference_response.models import (
    model_inference_response_projection,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
    ModelSavingsEstimatedEvent,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
)
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
    async_house_tenant_write_uuid,
    sync_house_tenant_write_uuid,
)

SAVINGS_ESTIMATED_TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"
_DRIFTED_UUID = "0e9a1f5c-7d3b-4c2a-9f10-5b6d7e8f9a01"
_UNKNOWN_TENANT_UUID = "a1b2c3d4-0000-4000-8000-000000000001"
_CONFIGURED_TENANT_SLUG = "tenant-19438-configured"
_CONFIGURED_TENANT_UUID = UUID("19438000-0000-4000-8000-000000000001")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _savings_event(session_id: str) -> ModelSavingsEstimatedEvent:
    return ModelSavingsEstimatedEvent(
        event_timestamp=datetime(2026, 9, 24, 17, 28, tzinfo=UTC),
        session_id=session_id,
        model_local="qwen3-coder-30b",
        model_cloud_baseline="claude-opus-4",
        local_cost_usd=Decimal("0.000000"),
        cloud_cost_usd=Decimal("0.500000"),
        savings_usd=Decimal("0.500000"),
        repo_name="omnimarket",
        machine_id="m-201",
    )


class _RegistryDb(InmemoryDatabaseAdapter):
    """An in-memory adapter whose ``tenant_registry_mirror`` answers for the slug."""

    def __init__(self, mirror: dict[str, str]) -> None:
        super().__init__()
        self._mirror = mirror

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if table == "tenant_registry_mirror":
            filters = filters or {}
            slug = filters.get("tenant_slug")
            uuid_value = filters.get("tenant_uuid")
            for known_slug, known_uuid in self._mirror.items():
                if slug == known_slug or uuid_value == known_uuid:
                    return [{"tenant_slug": known_slug, "tenant_uuid": known_uuid}]
            return []
        return super().query(
            table, filters, order_by=order_by, descending=descending, limit=limit
        )


def _meta() -> MessageMeta:
    return MessageMeta(
        topic=SAVINGS_ESTIMATED_TOPIC, partition=0, offset=1, fallback_id=str(uuid4())
    )


def _savings_estimated_record_without_tenant() -> dict[str, Any]:
    payload = {
        "source_event_id": str(uuid4()),
        "session_id": str(uuid4()),
        "actual_model_id": "glm-5.2",
        "counterfactual_model_id": "claude-opus-4-6",
        "actual_cost_usd": "0.001000",
        "estimated_total_savings_usd": "0.500000",
        "timestamp_iso": "2026-09-24T17:28:00+00:00",
        "is_measured": True,
        "usage_source": "measured",
        "pricing_manifest_version": "2026-09-01",
    }
    unwrapped: dict[str, Any] = dict(payload)
    unwrapped["_envelope"] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "envelope_timestamp": "2026-09-24T17:28:00+00:00",
        "correlation_id": str(uuid4()),
        "event_type": "omnibase-infra.savings-estimated",
        "tenant_id": None,
    }
    return unwrapped


def _async_db(*, registry_uuid: str | None) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(
        return_value=UUID(registry_uuid) if registry_uuid is not None else None
    )
    return db


def _insert_call(db: AsyncMock) -> Any:
    writes = [
        call
        for call in db.execute.await_args_list
        if "INSERT INTO savings_estimates" in str(call.args[0])
    ]
    assert writes, "expected a savings_estimates INSERT"
    return writes[-1]


@pytest.mark.unit
class TestSyncSavingsWriterStampsTheRegistryUuid:
    """AC1, sync writer (``HandlerProjectionSavings.project``)."""

    def test_unattributed_event_lands_under_the_registry_uuid(self) -> None:
        db = _RegistryDb({HOUSE_TENANT_SLUG: str(HOUSE_TENANT_UUID)})
        HandlerProjectionSavings().project(_savings_event("sess-19438-a"), db)
        rows = InmemoryDatabaseAdapter.query(db, "savings_estimates")
        assert len(rows) == 1
        tenant = rows[0]["tenant_id"]
        assert tenant != HOUSE_TENANT_SLUG, "the house SLUG must never be stored"
        assert _is_uuid(tenant)
        assert tenant == str(HOUSE_TENANT_UUID)

    def test_lane_without_a_mirror_row_uses_the_closed_legacy_uuid(self) -> None:
        db = _RegistryDb({})
        HandlerProjectionSavings().project(_savings_event("sess-19438-b"), db)
        rows = InmemoryDatabaseAdapter.query(db, "savings_estimates")
        assert rows[0]["tenant_id"] == str(HOUSE_TENANT_UUID)

    def test_registry_drift_is_a_typed_refusal_and_writes_nothing(self) -> None:
        """Positive control for the refusal: a mirror that disagrees with the
        closed mapping for the house slug is drift, and the write is refused
        before any row exists."""
        db = _RegistryDb({HOUSE_TENANT_SLUG: _DRIFTED_UUID})
        with pytest.raises(TenantRegistryResolutionError):
            HandlerProjectionSavings().project(_savings_event("sess-19438-c"), db)
        assert InmemoryDatabaseAdapter.query(db, "savings_estimates") == []

    def test_configured_tenant_unknown_to_the_registry_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.config import settings as settings_module

        monkeypatch.setenv("ONEX_TENANT_ID", _UNKNOWN_TENANT_UUID)
        settings_module.get_settings.cache_clear()
        try:
            db = _RegistryDb({HOUSE_TENANT_SLUG: str(HOUSE_TENANT_UUID)})
            with pytest.raises(TenantRegistryResolutionError):
                sync_house_tenant_write_uuid(db, table="savings_estimates")
        finally:
            monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
            settings_module.get_settings.cache_clear()

    def test_configured_tenant_slug_resolves_to_its_registry_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.config import settings as settings_module

        monkeypatch.setenv("ONEX_TENANT_ID", _CONFIGURED_TENANT_SLUG)
        settings_module.get_settings.cache_clear()
        try:
            resolved = sync_house_tenant_write_uuid(
                _RegistryDb({_CONFIGURED_TENANT_SLUG: str(_CONFIGURED_TENANT_UUID)}),
                table="savings_estimates",
            )
            assert resolved == str(_CONFIGURED_TENANT_UUID)
            assert resolved != _CONFIGURED_TENANT_SLUG
            assert resolved != str(HOUSE_TENANT_UUID)
        finally:
            monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
            settings_module.get_settings.cache_clear()

    def test_mirror_lookup_passes_a_uuid_to_the_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.config import settings as settings_module
        from omnimarket.projection import (
            tenant_registry_resolution as resolution_module,
        )

        real_resolver = resolution_module.resolve_registry_tenant_uuid
        recorded_registry_uuids: list[UUID | None] = []

        def resolver_spy(
            tenant_identity: str | None,
            *,
            registry_uuid: UUID | None,
        ) -> UUID:
            recorded_registry_uuids.append(registry_uuid)
            return real_resolver(tenant_identity, registry_uuid=registry_uuid)

        monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
        monkeypatch.setattr(
            resolution_module, "resolve_registry_tenant_uuid", resolver_spy
        )
        settings_module.get_settings.cache_clear()
        try:
            sync_house_tenant_write_uuid(
                _RegistryDb({HOUSE_TENANT_SLUG: str(HOUSE_TENANT_UUID)}),
                table="savings_estimates",
            )
            assert len(recorded_registry_uuids) == 1
            registry_uuid = recorded_registry_uuids[0]
            assert isinstance(registry_uuid, UUID)
            assert not isinstance(registry_uuid, str)
            assert registry_uuid == HOUSE_TENANT_UUID
        finally:
            settings_module.get_settings.cache_clear()


@pytest.mark.unit
class TestAsyncSavingsRunnerStampsTheRegistryUuid:
    """AC1, the deployed Kafka writer (``SavingsProjectionRunner``)."""

    def test_unattributed_event_lands_under_the_registry_uuid(self) -> None:
        db = _async_db(registry_uuid=str(HOUSE_TENANT_UUID))
        runner = SavingsProjectionRunner()
        runner._db = db
        ok = asyncio.run(
            runner.project_event(
                SAVINGS_ESTIMATED_TOPIC,
                _savings_estimated_record_without_tenant(),
                _meta(),
            )
        )
        assert ok is True
        call = _insert_call(db)
        assert call.kwargs["tenant"] == str(HOUSE_TENANT_UUID)
        assert str(HOUSE_TENANT_UUID) in call.args[1:]
        assert HOUSE_TENANT_SLUG not in call.args[1:]

    def test_registry_drift_refuses_before_any_insert(self) -> None:
        db = _async_db(registry_uuid=_DRIFTED_UUID)
        runner = SavingsProjectionRunner()
        runner._db = db
        with pytest.raises(TenantRegistryResolutionError):
            asyncio.run(
                runner.project_event(
                    SAVINGS_ESTIMATED_TOPIC,
                    _savings_estimated_record_without_tenant(),
                    _meta(),
                )
            )
        writes = [
            call
            for call in db.execute.await_args_list
            if "INSERT INTO savings_estimates" in str(call.args[0])
        ]
        assert writes == []

    def test_async_resolver_answers_the_uuid(self) -> None:
        db = _async_db(registry_uuid=str(HOUSE_TENANT_UUID))
        resolved = asyncio.run(
            async_house_tenant_write_uuid(db, table="savings_estimates")
        )
        assert resolved == str(HOUSE_TENANT_UUID)

    def test_configured_tenant_slug_resolves_to_its_registry_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.config import settings as settings_module

        monkeypatch.setenv("ONEX_TENANT_ID", _CONFIGURED_TENANT_SLUG)
        settings_module.get_settings.cache_clear()
        try:
            resolved = asyncio.run(
                async_house_tenant_write_uuid(
                    _async_db(registry_uuid=str(_CONFIGURED_TENANT_UUID)),
                    table="savings_estimates",
                )
            )
            assert resolved == str(_CONFIGURED_TENANT_UUID)
            assert resolved != _CONFIGURED_TENANT_SLUG
            assert resolved != str(HOUSE_TENANT_UUID)
        finally:
            monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
            settings_module.get_settings.cache_clear()


@pytest.mark.unit
class TestTheNamedProjectionDefaultsAreUuids:
    """AC2: none of the three named defaults resolves to the slug."""

    def test_savings_house_default_parses_as_uuid(self) -> None:
        resolved = sync_house_tenant_write_uuid(
            _RegistryDb({}), table="savings_estimates"
        )
        assert _is_uuid(resolved)

    def test_delegation_projection_default_parses_as_uuid(self) -> None:
        assert handler_projection_delegation.DEFAULT_TENANT != HOUSE_TENANT_SLUG
        assert _is_uuid(handler_projection_delegation.DEFAULT_TENANT)
        assert str(HOUSE_TENANT_UUID) == handler_projection_delegation.DEFAULT_TENANT

    def test_inference_response_default_parses_as_uuid(self) -> None:
        default = model_inference_response_projection.DEFAULT_TENANT
        assert default != HOUSE_TENANT_SLUG
        assert _is_uuid(default)
        assert default == str(HOUSE_TENANT_UUID)
