# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15126 acceptance chain: real Postgres demand-query EFFECT feeding the
pure liveness-evaluate COMPUTE, proving the §2 A9 proof bar verbatim:

    "Input event -> terminal event -> exact projection key/value. Balanced
    topic offsets cannot certify liveness."

This is the real seam (`HandlerLivenessDemandQueryEffect` against a live
Postgres connection -> `HandlerLivenessEvaluateCompute`'s pure decision),
never a surrogate/mock of either handler. All 5 states
(NOT_READY/NO_DEMAND/HEALTHY/STALE/RED) are driven end to end against a
real, isolated Postgres schema (own schema/table, self-contained
connect-or-skip -- mirrors the accepted pattern in
tests/test_projection_delegation_tier_distribution_omn13662.py -- so this
test never touches production `event_ledger` data).

Runs only under ``-m integration`` with a reachable Postgres (self-skips
otherwise, never errors, matching the repo's connect-or-skip convention).

Related:
    - OMN-15126: demand-aware liveness evaluator (acceptance criteria)
    - OMN-14845: design (docs/design/2026-07-20-demand-aware-liveness-state-machine-design.md)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest
from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from omnibase_core.models.runtime.model_demand_source_ref import ModelDemandSourceRef
from omnibase_core.models.runtime.model_liveness_artifact_ref import ModelArtifactRef
from omnibase_core.models.runtime.model_liveness_registry_entry import (
    ModelLivenessRegistryEntry,
)
from omnibase_core.models.runtime.model_output_join_spec import ModelOutputJoinSpec

from omnimarket.nodes.node_liveness_demand_query_effect.handlers.handler_liveness_demand_query_effect import (
    HandlerLivenessDemandQueryEffect,
)
from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_demand_query_request import (
    ModelLivenessDemandQueryRequest,
)
from omnimarket.nodes.node_liveness_evaluate_compute.handlers.handler_liveness_evaluate_compute import (
    HandlerLivenessEvaluateCompute,
)
from omnimarket.nodes.node_liveness_evaluate_compute.models.model_liveness_evaluate_request import (
    ModelLivenessEvaluateRequest,
)

_INPUT_TOPIC = "onex.cmd.test.omn15126-liveness-input.v1"
_TERMINAL_TOPIC = "onex.evt.test.omn15126-liveness-terminal.v1"
_NO_DEMAND_TOPIC = "onex.cmd.test.omn15126-liveness-no-such-input.v1"


async def _connect_or_skip() -> Any:
    """Real asyncpg connection, or SKIP (never ERROR) with no reachable DB.

    Self-contained rather than the shared ``postgres_fixture`` so this test
    skips cleanly under plain ``-m integration`` without also requiring
    fixture teardown ordering, matching
    test_projection_delegation_tier_distribution_omn13662.py's own pattern.
    """
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping OMN-15126 liveness DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"no reachable Postgres for OMN-15126 liveness DB proof: {exc}")


def _registry_entry(
    *,
    eligibility_predicate: str = f"topic = '{_INPUT_TOPIC}'",
    locator: str = "event_ledger",
) -> ModelLivenessRegistryEntry:
    return ModelLivenessRegistryEntry(
        surface_id="omnimarket.test_omn15126_liveness_surface",
        owner="omn15126-acceptance-test",
        lane="dev",
        demand_source=ModelDemandSourceRef(
            kind="table_query",
            locator=locator,
            eligibility_predicate=eligibility_predicate,
        ),
        expected_output_join=ModelOutputJoinSpec(
            terminal_topic=_TERMINAL_TOPIC,
            projection_table="event_ledger",
            projection_key_fields=("correlation_id",),
            projection_key_canonicalization="json_sorted_keys",
            expected_value_predicate=f"topic = '{_TERMINAL_TOPIC}'",
        ),
        artifact_ref=ModelArtifactRef(
            repo="OmniNode-ai/omnimarket",
            contract_path="src/omnimarket/nodes/node_liveness_evaluate_compute/contract.yaml",
        ),
        freshness_slo_seconds=300,
        error_budget_ratio=None,  # zero tolerance (design §3.2 step 3)
    )


def _base_evaluate_kwargs(*, evaluated_at: datetime) -> dict[str, Any]:
    return {
        "surface_id": "omnimarket.test_omn15126_liveness_surface",
        "lane": "dev",
        "deployed_sha": "abc123",
        "image_digest": "sha256:deadbeef",
        "config_digest": "sha256:cafef00d",
        "runner": "node_liveness_evaluate_compute@omn15126-acceptance-test",
        "evaluated_at": evaluated_at,
        "freshness_window_seconds": 300,
        "error_budget_ratio": 0.0,
    }


@pytest.mark.integration
async def test_liveness_evaluator_chain_proves_all_five_states() -> None:
    """Drive NOT_READY / RED / HEALTHY(red-then-green) / NO_DEMAND / STALE end
    to end through the real EFFECT -> COMPUTE chain against live Postgres.
    """
    conn = await _connect_or_skip()
    schema = "omn15126_liveness_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        # Minimal event_ledger-shaped fixture table -- same idempotency-key
        # shape as the real omnibase_infra event_ledger (topic/partition/
        # kafka_offset/correlation_id/ledger_written_at), isolated to this
        # test's own schema so production data is never touched.
        await conn.execute(
            """
            CREATE TABLE event_ledger (
                ledger_entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL DEFAULT 0,
                kafka_offset BIGINT NOT NULL DEFAULT 0,
                correlation_id UUID,
                ledger_written_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        effect = HandlerLivenessDemandQueryEffect(pool=_SingleConnPool(conn))
        compute = HandlerLivenessEvaluateCompute()

        # ------------------------------------------------------------------
        # 1. NOT_READY -- demand source query itself fails (missing table).
        # ------------------------------------------------------------------
        not_ready_entry = _registry_entry(locator="event_ledger_missing_table")
        not_ready_result = await effect.handle(
            ModelLivenessDemandQueryRequest(registry_entry=not_ready_entry)
        )
        assert not_ready_result.query_succeeded is False
        assert "demand query failed" in (not_ready_result.error_message or "")

        not_ready_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=_utcnow()),
                demand_query_succeeded=False,
                not_ready_reason=not_ready_result.error_message,
            )
        )
        assert not_ready_receipt.state == EnumLivenessState.NOT_READY
        assert "demand query failed" in (not_ready_receipt.not_ready_reason or "")

        # ------------------------------------------------------------------
        # 2. RED -- eligible demand exists, its correlated join fails (no
        #    terminal row exists yet for this correlation_id).
        # ------------------------------------------------------------------
        red_correlation_id = uuid4()
        await conn.execute(
            "INSERT INTO event_ledger (topic, partition, kafka_offset, correlation_id) "
            "VALUES ($1, 0, 1, $2)",
            _INPUT_TOPIC,
            red_correlation_id,
        )

        entry = _registry_entry()
        red_query = await effect.handle(
            ModelLivenessDemandQueryRequest(registry_entry=entry)
        )
        assert red_query.query_succeeded is True
        assert red_query.eligible_count == 1
        assert red_query.checked_count == 1
        assert red_query.failed_count == 1
        assert red_query.failed_sample is not None
        assert red_query.failed_sample.correlation_id == red_correlation_id
        assert red_query.healthy_sample is None

        red_evaluated_at = _utcnow()
        red_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=red_evaluated_at),
                eligible_count=red_query.eligible_count,
                checked_count=red_query.checked_count,
                failed_count=red_query.failed_count,
                correlation_id=red_query.failed_sample.correlation_id,
                input_event_ref=red_query.failed_sample.input_event_ref,
                projection_key_canonical=red_query.failed_sample.projection_key_canonical,
                expected_value_predicate_result=False,
            )
        )
        assert red_receipt.state == EnumLivenessState.RED
        assert red_receipt.correlation_id == red_correlation_id
        assert red_receipt.terminal_event_ref is None
        assert red_receipt.failure_detail is not None

        # ------------------------------------------------------------------
        # 3. HEALTHY (red-then-green) -- insert the matching terminal row for
        #    the SAME correlation_id, re-run, the join now succeeds.
        # ------------------------------------------------------------------
        await conn.execute(
            "INSERT INTO event_ledger (topic, partition, kafka_offset, correlation_id) "
            "VALUES ($1, 0, 2, $2)",
            _TERMINAL_TOPIC,
            red_correlation_id,
        )

        healthy_query = await effect.handle(
            ModelLivenessDemandQueryRequest(registry_entry=entry)
        )
        assert healthy_query.query_succeeded is True
        assert healthy_query.eligible_count == 1
        assert healthy_query.checked_count == 1
        assert healthy_query.failed_count == 0
        assert healthy_query.healthy_sample is not None
        assert healthy_query.healthy_sample.correlation_id == red_correlation_id
        assert healthy_query.healthy_sample.expected_value_predicate_result is True

        healthy_evaluated_at = red_evaluated_at + timedelta(seconds=5)
        healthy_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=healthy_evaluated_at),
                eligible_count=healthy_query.eligible_count,
                checked_count=healthy_query.checked_count,
                failed_count=healthy_query.failed_count,
                correlation_id=healthy_query.healthy_sample.correlation_id,
                input_event_ref=healthy_query.healthy_sample.input_event_ref,
                terminal_event_ref=healthy_query.healthy_sample.terminal_event_ref,
                projection_key_canonical=healthy_query.healthy_sample.projection_key_canonical,
                projection_value_hash=healthy_query.healthy_sample.projection_value_hash,
                projection_expected_value_hash=(
                    healthy_query.healthy_sample.projection_expected_value_hash
                ),
                expected_value_predicate_result=(
                    healthy_query.healthy_sample.expected_value_predicate_result
                ),
            )
        )
        assert healthy_receipt.state == EnumLivenessState.HEALTHY
        assert healthy_receipt.correlation_id == red_correlation_id
        assert healthy_receipt.terminal_event_ref is not None
        assert healthy_receipt.terminal_event_ref.topic == _TERMINAL_TOPIC
        assert healthy_receipt.projection_value_hash is not None
        # red-then-green: same correlation_id, RED then HEALTHY across the fix.
        assert red_receipt.correlation_id == healthy_receipt.correlation_id

        # ------------------------------------------------------------------
        # 4. NO_DEMAND -- zero eligible demand this cycle, but a fresh
        #    HEALTHY receipt exists from step 3 (negative control: quiet but
        #    correct surface must NOT read HEALTHY or RED).
        # ------------------------------------------------------------------
        no_demand_entry = _registry_entry(
            eligibility_predicate=f"topic = '{_NO_DEMAND_TOPIC}'"
        )
        no_demand_query = await effect.handle(
            ModelLivenessDemandQueryRequest(registry_entry=no_demand_entry)
        )
        assert no_demand_query.query_succeeded is True
        assert no_demand_query.eligible_count == 0

        no_demand_evaluated_at = healthy_evaluated_at + timedelta(seconds=30)
        no_demand_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=no_demand_evaluated_at),
                eligible_count=0,
                demand_query_evidence=no_demand_query.demand_query_evidence,
                prior_healthy_receipt_id=healthy_receipt.receipt_id,
                prior_healthy_at=healthy_receipt.evaluated_at,
            )
        )
        assert no_demand_receipt.state == EnumLivenessState.NO_DEMAND
        assert no_demand_receipt.state != EnumLivenessState.HEALTHY
        assert no_demand_receipt.state != EnumLivenessState.RED

        # ------------------------------------------------------------------
        # 5. STALE -- zero eligible demand AND no fresh prior HEALTHY (either
        #    never-healthy, or the prior HEALTHY has aged past the freshness
        #    window).
        # ------------------------------------------------------------------
        stale_never_healthy_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=_utcnow()),
                eligible_count=0,
                demand_query_evidence=no_demand_query.demand_query_evidence,
            )
        )
        assert stale_never_healthy_receipt.state == EnumLivenessState.STALE
        assert stale_never_healthy_receipt.last_healthy_receipt_id is None

        aged_out_evaluated_at = healthy_evaluated_at + timedelta(seconds=301)
        stale_aged_out_receipt = await compute.handle(
            ModelLivenessEvaluateRequest(
                **_base_evaluate_kwargs(evaluated_at=aged_out_evaluated_at),
                eligible_count=0,
                demand_query_evidence=no_demand_query.demand_query_evidence,
                prior_healthy_receipt_id=healthy_receipt.receipt_id,
                prior_healthy_at=healthy_receipt.evaluated_at,
            )
        )
        assert stale_aged_out_receipt.state == EnumLivenessState.STALE
        assert (
            stale_aged_out_receipt.last_healthy_receipt_id == healthy_receipt.receipt_id
        )
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _SingleConnPool:
    """Wrap a single asyncpg.Connection with the .acquire() pool interface.

    HandlerLivenessDemandQueryEffect is written against an asyncpg.Pool (for
    real deployments), but this test drives one already-connected,
    schema-scoped connection through the whole chain -- this thin adapter
    lets the SAME production handler code run unmodified against it.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class _AcquireContext:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc_info: object) -> None:
        return None
