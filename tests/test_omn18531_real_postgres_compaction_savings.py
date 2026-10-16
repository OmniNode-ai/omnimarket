# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18531: a compaction saving lands on real Postgres beside delegation's.

The unit suite (``tests/test_omn18531_compaction_savings.py``) proves the
measure and the PostCompact branch against an asyncpg double. This module
drives PostCompact hook events through the REAL ``SavingsProjectionRunner``
write path against a migrated schema, so the compaction row is decided by the
table's real column types, its amounts check and its unique key, and is read
back through the delegation savings view.

Provisioned by the OMN-20303 helper on the INTEGRATION_POSTGRES_* DSN, in a
throwaway schema; SKIPS (never ERRORs) without a reachable database.
"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_savings.handlers.handler_compaction_savings import (
    COMPACTION_MOD_MODEL,
    COMPACTION_TASK_TYPE,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID
from tests.test_omn20303_real_postgres_savings_one_row_per_run import (
    _provisioned_runner,
)

HOOK_EVENT_TOPIC = "onex.evt.omniclaude.hook-event.v1"
SESSION = "4fb6b47e-ec68-4a15-ab71-0de49ccbcec6"


def _post_compact(summary_length: int) -> dict[str, object]:
    return {
        "event_id": str(uuid4()),
        "schema_version": "1.0.0",
        "hook_event_name": "PostCompact",
        "emitted_at": "2026-10-15T04:01:16.352869+00:00",
        "actor": "claude",
        "host": "h101",
        "lineage": {"session_id": SESSION},
        "payload": {
            "hook_event_name": "PostCompact",
            "trigger": "manual",
            "context_tokens": 100_000,
            "model": "claude-sonnet-5-5",
            "compact_summary_ref": {
                "field": "compact_summary",
                "length": summary_length,
                "sha256": "0" * 64,
                "content_record_id": str(uuid4()),
            },
        },
        "content_refs": [],
    }


@pytest.mark.integration
async def test_real_postgres_mod_answered_compaction_is_a_savings_row() -> None:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD")
    ):
        pytest.skip("no INTEGRATION_POSTGRES_PASSWORD -- skipping OMN-18531 test")
    answered = _post_compact(summary_length=0)
    native = _post_compact(summary_length=18_854)
    async with _provisioned_runner() as (runner, admin_conn):
        for offset, data in enumerate((answered, native)):
            meta = MessageMeta(
                partition=0,
                offset=offset,
                fallback_id=str(data["event_id"]),
                topic=HOOK_EVENT_TOPIC,
            )
            assert await runner.project_event(HOOK_EVENT_TOPIC, dict(data), meta)

        rows = await admin_conn.fetch(
            "SELECT session_id, model_local, model_cloud_baseline, cloud_cost_usd, "
            "local_cost_usd, savings_usd, task_type, prompt_tokens, "
            "completion_tokens, savings_method, usage_source, tenant_id "
            "FROM savings_estimates"
        )
        # The aggregate the delegation savings surface serves
        # (delegation.savings.v1) counts the compaction with no view change.
        surfaced = await admin_conn.fetch(
            "SELECT cumulative_savings_usd, session_count "
            "FROM projection_delegation_savings WHERE tenant_id = $1",
            str(HOUSE_TENANT_UUID),
        )

    assert len(rows) == 1, "only the compaction the mod answered is a saving"
    row = rows[0]
    assert row["session_id"] == answered["event_id"]
    assert row["model_local"] == COMPACTION_MOD_MODEL
    assert row["model_cloud_baseline"] == "claude-sonnet-5-5"
    assert row["cloud_cost_usd"] == Decimal("0.200000")
    assert row["local_cost_usd"] == Decimal("0.000000")
    assert row["savings_usd"] == Decimal("0.200000")
    assert row["task_type"] == COMPACTION_TASK_TYPE
    assert (row["prompt_tokens"], row["completion_tokens"]) == (100_000, 0)
    assert (row["savings_method"], row["usage_source"]) == ("measured", "measured")
    assert row["tenant_id"] == str(HOUSE_TENANT_UUID)
    assert len(surfaced) == 1
    assert Decimal(str(surfaced[0]["cumulative_savings_usd"])) == Decimal("0.2")
    assert surfaced[0]["session_count"] == 1
