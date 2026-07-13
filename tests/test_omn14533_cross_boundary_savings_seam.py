# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for OMN-14533.

Drives the REAL producer model — omnibase_infra's
node_savings_estimation_compute.models.model_savings_estimate.ModelSavingsEstimate
— through node_projection_savings' two live consumer entrypoints
(SavingsProjectionRunner.project_event, the deployed Kafka->Postgres runner,
and HandlerProjectionSavings.handle, the RuntimeLocal/contract-declared
event_model shim). Not a hand-rolled stand-in for either side.

RED (documented — see PR description / git history): before OMN-14533, ONE
producer model (ModelSavingsEstimate: source_event_id, actual_model_id,
counterfactual_model_id, actual_cost_usd, estimated_total_savings_usd,
timestamp_iso, ...) fed TWO consumer field vocabularies
(session_id/model_local/model_cloud_baseline/local_cost_usd/cloud_cost_usd/
savings_usd/event_timestamp) that shared almost no field names. Every real
savings-estimated.v1 message failed the runner's "missing model identifiers"
/ "missing cost fields" DLQ checks (0 rows ever, DLQ growing), and the
strict RuntimeLocal-shim model (extra="forbid") would raise a ValidationError
carrying one error per missing required field plus one per rejected extra
key.

GREEN: both consumer entrypoints now normalize the real shape and upsert.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omnibase_infra.nodes.node_savings_estimation_compute.models.model_savings_estimate import (
    ModelSavingsEstimate,
)

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

SAVINGS_ESTIMATED_TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"
SAVINGS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-savings-malformed.v1"


def _real_savings_estimate() -> ModelSavingsEstimate:
    """A ModelSavingsEstimate built the way node_savings_estimation_compute
    actually builds one — every field named exactly as the real producer
    names it, not a canonical-shaped stand-in."""
    return ModelSavingsEstimate(
        session_id="sess-omn14533",
        actual_total_tokens=1500,
        actual_cost_usd=0.0,
        actual_model_id="qwen3-coder-30b",
        counterfactual_model_id="claude-opus-4-8",
        direct_savings_usd=0.05,
        heuristic_savings_usd=0.0,
        estimated_total_savings_usd=0.05,
        is_measured=True,
    )


def _mock_db() -> Any:
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=None)
    return mock_db


def _capture() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def capture_publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, capture_publish


@pytest.mark.unit
class TestSavingsRunnerRealProducerShape:
    """SavingsProjectionRunner (the deployed Kafka->Postgres consumer)."""

    def test_real_savings_estimate_upserts_not_dlq(self) -> None:
        published, capture = _capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        data = _real_savings_estimate().model_dump(mode="json")
        meta = MessageMeta(partition=0, offset=0, fallback_id="omn14533")

        ok = asyncio.run(runner.project_event(SAVINGS_ESTIMATED_TOPIC, data, meta))

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], (
            f"real savings-estimated.v1 payload must NOT be DLQ'd; "
            f"got: {[v for _, v in dlq_rows]}"
        )
        runner._db.execute.assert_awaited_once()
        args = runner._db.execute.await_args.args
        # positional args to the INSERT: sql, event_timestamp, session_id,
        # model_local, model_cloud_baseline, local_cost_usd, cloud_cost_usd,
        # savings_usd, repo_name, machine_id
        _, _event_timestamp, session_id, model_local, model_cloud_baseline = args[:5]
        local_cost_usd, cloud_cost_usd, savings_usd = args[5:8]
        assert session_id == "sess-omn14533"
        assert model_local == "qwen3-coder-30b"
        assert model_cloud_baseline == "claude-opus-4-8"
        assert local_cost_usd == Decimal("0.0")
        assert savings_usd == Decimal("0.05")
        assert cloud_cost_usd == local_cost_usd + savings_usd

    def test_delegate_skill_and_canonical_paths_unaffected(self) -> None:
        """The normalization is a no-op for payloads that already carry
        model_local (canonical shape) — regression guard against the
        normalizer accidentally firing on unrelated topics."""
        from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
            _normalize_savings_estimate_payload,
        )

        canonical = {
            "session_id": "s1",
            "model_local": "already-canonical",
            "model_cloud_baseline": "claude",
            "local_cost_usd": "1.0",
            "cloud_cost_usd": "2.0",
            "savings_usd": "1.0",
        }
        assert _normalize_savings_estimate_payload(canonical) == canonical


@pytest.mark.unit
class TestHandlerProjectionSavingsRealProducerShape:
    """HandlerProjectionSavings.handle() — the contract-declared event_model
    shim (RuntimeLocal / onex run-node path)."""

    def test_real_savings_estimate_projects_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionSavings()
        payload: dict[str, object] = {
            "_db": db,
            **_real_savings_estimate().model_dump(mode="json"),
        }

        result = handler.handle(payload)

        assert result["rows_upserted"] == 1
        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-omn14533"
        assert rows[0]["model_local"] == "qwen3-coder-30b"
        assert rows[0]["model_cloud_baseline"] == "claude-opus-4-8"
        assert rows[0]["savings_usd"] == Decimal("0.05")

    def test_real_savings_estimate_timestamp_defaults_to_utc(self) -> None:
        """timestamp_iso (ModelSavingsEstimate's real timestamp field) must
        thread through as event_timestamp, not fall back to a naive default
        that the tz-aware validator would reject."""
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionSavings()
        estimate = _real_savings_estimate()
        payload: dict[str, object] = {"_db": db, **estimate.model_dump(mode="json")}

        result = handler.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("savings_estimates")[0]
        parsed = datetime.fromisoformat(row["event_timestamp"])
        assert parsed.tzinfo is not None
        # ModelSavingsEstimate.timestamp_iso defaults to "now" — assert it
        # landed within a wide sanity window rather than exact-matching a
        # freshly re-generated default_factory value.
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60
