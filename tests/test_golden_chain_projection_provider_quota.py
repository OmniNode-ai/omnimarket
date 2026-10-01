# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: provider quota observations fold into durable per-key state.

The pure fold derives, per observation, the model row and the provider-wide
row; the effect writer persists both under the event tenant. The merge SQL is
proven against Postgres in ``test_omn20154_provider_quota_real_postgres``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.provider_quota import (
    PROVIDER_WIDE_MODEL_SCOPE,
    EnumProviderQuotaOutcome,
)
from omnimarket.nodes.node_projection_provider_quota.handlers import (
    HandlerProjectionProviderQuota,
    ProviderQuotaProjectionWriter,
)
from omnimarket.nodes.node_projection_provider_quota.models import (
    ModelProviderQuotaProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit
_IN_TOPIC = "onex.evt.omnimarket.provider-quota-observed.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-provider-quota-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-provider-quota-malformed.v1"  # onex-topic-allow: asserted against the contract
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_provider_quota"
)
_T0 = datetime(2026, 9, 30, 13, 42, tzinfo=UTC)
_TENANT = "11111111-1111-1111-1111-111111111111"


def _event(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "tenant_id": _TENANT,
        "credential_ref": "llm.glm.api_key",
        "provider_id": "zai",
        "model_name": "glm-5.3",
        "outcome": "call_ok",
        "http_status": 200,
        "call_started_at": _T0 - timedelta(seconds=2),
        "observed_at": _T0,
        "source": "runtime_orchestrator",
    } | updates


def _hit(**updates: Any) -> dict[str, Any]:
    return (
        _event(
            outcome="limit_hit",
            http_status=429,
            provider_code="1302",
            disposition="cooldown",
            block_scope="provider",
            blocked_until=_T0 + timedelta(seconds=60),
            reason="zai code 1302: capacity refusal",
        )
        | updates
    )


class _Store:
    """Loop-bound adapter double that records each upsert's bound arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.tenants: list[str | None] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert (
            "ON CONFLICT (tenant_id, credential_ref, provider_id, model_scope)" in query
        )
        assert "WHERE t.observed_at < EXCLUDED.observed_at" in query
        assert "RETURNING projection_cursor" in query
        self.calls.append(params)
        self.tenants.append(tenant)
        return [{"projection_cursor": len(self.calls)}]


def _writer() -> tuple[ProviderQuotaProjectionWriter, _Store]:
    writer = ProviderQuotaProjectionWriter()
    store = _Store()
    writer._db = store  # type: ignore[assignment]
    return writer, store


def test_a_call_counts_on_the_model_row_and_the_provider_row() -> None:
    fold = HandlerProjectionProviderQuota()
    rows = fold.handle(
        ModelProviderQuotaProjectionRequest.model_validate(_event())
    ).rows
    assert [r.model_scope for r in rows] == ["glm-5.3", PROVIDER_WIDE_MODEL_SCOPE]
    for row in rows:
        assert row.tenant_id == UUID(_TENANT)
        assert (row.credential_ref, row.provider_id) == ("llm.glm.api_key", "zai")
        assert row.counts_hit is False
        assert row.sets_block is False
        # A successful call clears any block recorded before it was SENT.
        assert row.clears_blocks_before == _T0 - timedelta(seconds=2)


def test_a_provider_scoped_hit_blocks_only_the_provider_row() -> None:
    rows = (
        HandlerProjectionProviderQuota()
        .handle(ModelProviderQuotaProjectionRequest.model_validate(_hit()))
        .rows
    )
    model_row, provider_row = rows
    assert model_row.counts_hit
    assert provider_row.counts_hit
    assert model_row.sets_block is False
    assert provider_row.sets_block is True
    assert provider_row.disposition == "cooldown"
    assert provider_row.blocked_until == _T0 + timedelta(seconds=60)
    assert model_row.clears_blocks_before is None


def test_a_model_scoped_hit_blocks_only_the_model_row() -> None:
    model_row, provider_row = (
        HandlerProjectionProviderQuota()
        .handle(
            ModelProviderQuotaProjectionRequest.model_validate(
                _hit(
                    provider_id="openrouter",
                    model_name="qwen/qwen3-coder:free",
                    block_scope="model",
                    provider_code="429",
                )
            )
        )
        .rows
    )
    assert model_row.sets_block is True
    assert provider_row.sets_block is False


def test_a_billing_refusal_is_indefinite() -> None:
    _, provider_row = (
        HandlerProjectionProviderQuota()
        .handle(
            ModelProviderQuotaProjectionRequest.model_validate(
                _hit(
                    provider_code="1113",
                    disposition="disable_until_billing",
                    blocked_until=None,
                    blocked_indefinitely=True,
                )
            )
        )
        .rows
    )
    assert provider_row.blocked_indefinitely is True
    assert provider_row.blocked_until is None


def test_the_writer_upserts_both_rows_under_the_event_tenant() -> None:
    writer, store = _writer()
    result = writer.handle(_hit())
    assert result["rows_upserted"] == 2
    assert store.tenants == [_TENANT, _TENANT]
    # Positional binding order is the SQL's contract; spot-check the key and
    # the block flag of each row.
    (model_call, provider_call) = store.calls
    assert model_call[:4] == (UUID(_TENANT), "llm.glm.api_key", "zai", "glm-5.3")
    assert provider_call[3] == PROVIDER_WIDE_MODEL_SCOPE
    assert model_call[10] is False
    assert provider_call[10] is True
    assert provider_call[6] == EnumProviderQuotaOutcome.LIMIT_HIT.value
    assert store.loop is None


def test_the_contract_declares_the_topics_the_writer_subscribes() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["event_bus"] == {
        "subscribe_topics": [_IN_TOPIC],
        "publish_topics": [_OUT_TOPIC],
        "dlq_topics": [_DLQ_TOPIC],
    }
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer()[0].subscribe_topics == [_IN_TOPIC]
    table = contract["db_io"]["db_tables"][0]
    assert (table["schema"], table["name"], table["access"]) == (
        "public",
        "provider_quota_state",
        "read_write",
    )
    assert {h["handler"]["name"] for h in contract["handler_routing"]["handlers"]} == {
        "ProviderQuotaProjectionWriter",
    }


def test_the_orchestrator_publishes_the_topic_the_projection_subscribes() -> None:
    orchestrator = yaml.safe_load(
        (_NODE.parent / "node_delegation_orchestrator" / "contract.yaml").read_text()
    )
    assert _IN_TOPIC in orchestrator["event_bus"]["publish_topics"]
    assert {
        "event_type": "ProviderQuotaObserved",
        "topic": _IN_TOPIC,
    }.items() <= next(
        e
        for e in orchestrator["published_events"]
        if e["event_type"] == "ProviderQuotaObserved"
    ).items()


def test_the_writer_declares_in_process_dispatch() -> None:
    assert issubclass(ProviderQuotaProjectionWriter, BaseProjectionRunner)
    assert ProviderQuotaProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert not hasattr(
        HandlerProjectionProviderQuota, "onex_runtime_inprocess_dispatch"
    )


def test_fold_determinism_and_envelope_timestamp() -> None:
    event = _event()
    event["_envelope_timestamp"] = event.pop("observed_at")
    request = ModelProviderQuotaProjectionRequest.model_validate(event)
    fold = HandlerProjectionProviderQuota()
    assert fold.handle(request) == fold.handle(request)
    assert fold.handle(request).rows[0].observed_at == _T0


@pytest.mark.parametrize(
    "broken",
    [
        {"outcome": "limit_hit"},  # a hit that names no block
        {"blocked_until": _T0},  # a block on a call that was not a hit
        {"model_name": "*"},  # the provider-wide scope is not a model
        {"tenant_id": None},
    ],
)
def test_malformed_observations_fail_closed(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelProviderQuotaProjectionRequest.model_validate(_event(**broken))


def test_migrations_enforce_tenant_isolation_and_writer_grants() -> None:
    ddl = (_NODE / "migrations/0000_create_provider_quota_state.sql").read_text()
    assert "tenant_id UUID NOT NULL" in ddl
    assert "PRIMARY KEY (tenant_id, credential_ref, provider_id, model_scope)" in ddl
    assert "ENABLE ROW LEVEL SECURITY" in ddl
    assert "FORCE ROW LEVEL SECURITY" not in ddl
    assert "CREATE POLICY tenant_isolation" in ddl
    force = (_NODE / "migrations/0002_force_rls_provider_quota_state.sql").read_text()
    assert "ALTER TABLE public.provider_quota_state FORCE ROW LEVEL SECURITY" in force
    grants = (
        _NODE
        / "migrations/0001_grant_tenant_projection_writer_provider_quota_state.sql"
    ).read_text()
    assert "GRANT SELECT, INSERT, UPDATE" in grants
    assert "ON SEQUENCE public.provider_quota_state_projection_cursor_seq" in grants
    assert "TO tenant_projection_writer" in grants
