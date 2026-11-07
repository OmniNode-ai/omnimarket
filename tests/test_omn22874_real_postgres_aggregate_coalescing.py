# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22874: real-Postgres proof that the coalesced aggregate republish reads the views.

The mock-DB tests in ``test_omn22874_delegation_writer_aggregate_coalescing.py``
prove the ordering: the offset commit does not wait on the aggregate views, and
each view is republished once per window. They cannot prove that the deferred
read still works, because the views are whole-table aggregates evaluated by
Postgres under ``FORCE ROW LEVEL SECURITY`` with the tenant in a GUC. This module
runs the deferred flush against the migrated schema, as the NOBYPASSRLS writer
role the OMN-18139 fixture builds, and reads what the flush published.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn18139_real_postgres_tenant_guc_representation import (
    _TENANT_UUID,
    _quality_gate_delivery,
    _rls_bound_runner,
    _seed_terminal_row,
)

BURST = 3


@pytest.mark.integration
class TestTheRunReachedARealDatabase:
    def test_the_connection_is_the_database_the_run_was_pointed_at(self) -> None:
        """Positive control: the schema below is built on the server named by
        INTEGRATION_POSTGRES_DB, not on a stand-in, so a pass is a real read."""

        async def _run() -> None:
            async with _rls_bound_runner() as (_runner, admin, schema):
                database = await admin.fetchval("SELECT current_database()")
                assert database == os.environ.get(
                    "INTEGRATION_POSTGRES_DB", "omnibase_infra"
                )
                views = await admin.fetchval(
                    "SELECT count(*) FROM information_schema.views "
                    "WHERE table_schema = $1 "
                    "AND table_name LIKE 'projection_delegation_%'",
                    schema,
                )
                assert views >= 4

        asyncio.run(_run())


@pytest.mark.integration
class TestTheDeferredRepublishReadsTheRealViews:
    def test_a_burst_applies_then_republishes_each_aggregate_once(self) -> None:
        async def _run() -> None:
            async with _rls_bound_runner() as (runner, admin, _schema):
                runner._aggregate_coalesce_window_s = 60.0
                runner.enable_aggregate_coalescing()
                views = len(runner._aggregate_exposures)
                assert views >= 2

                aggregate_topics = {e.topic for e in runner._aggregate_exposures}
                published: list[dict[str, Any]] = []

                async def _publish(exposure: Any, **kwargs: Any) -> bool:
                    # The per-row decisions snapshot still publishes with each
                    # write; only the aggregate exposures are deferred.
                    if exposure.topic in aggregate_topics:
                        published.append({"topic": exposure.topic, **kwargs})
                    return True

                runner.publish_snapshot_delta = _publish  # type: ignore[assignment]

                correlation_ids = [str(uuid4()) for _ in range(BURST)]
                for offset, correlation_id in enumerate(correlation_ids):
                    await _seed_terminal_row(admin, correlation_id)
                    ok = await runner.project_event(
                        runner._topic_quality_gate_result,
                        _quality_gate_delivery(correlation_id=correlation_id),
                        MessageMeta(
                            partition=0, offset=offset, fallback_id=correlation_id
                        ),
                    )
                    assert ok is True

                # Every verdict is written; no aggregate has been read or published.
                written = await admin.fetchval(
                    "SELECT count(*) FROM delegation_events "
                    "WHERE correlation_id = ANY($1::text[]) "
                    "AND score_source = 'deterministic_acceptance'",
                    correlation_ids,
                )
                assert written == BURST
                assert published == [], "the burst republished inside the window"

                await runner.drain_aggregates()

                assert len(published) == views, (
                    f"{len(published)} aggregate publishes for a burst of {BURST}; "
                    f"expected one per view ({views})"
                )
                for call in published:
                    assert call["tenant_id"] == _TENANT_UUID
                    assert str(call["row"]["tenant_id"]) == _TENANT_UUID

        asyncio.run(_run())
