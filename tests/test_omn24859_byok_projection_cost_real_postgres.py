# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24859: a customer-key call's priced cost lands in a real ``delegation_events`` row.

The in-memory test beside the projection node proves the pricing; this one drives the same
write path the deployed writer takes, ``DelegationProjectionRunner.project_event()``, against a
real, migrated Postgres schema (the ``INTEGRATION_POSTGRES_*`` database of the harness in
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``, which it reuses). The row the
tenant Runs list reads, and the R8 scenario's ``organic_run_has_values`` assertion reads, carries
``cost_usd`` above zero for ``glm-5.3-flash`` on ``byok-glm-general`` and an unpriced model is
still stored with no cost. The price comes from
``byok_actual_cost.byok_priced_actual_cost`` in the projection node's handlers package.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers import byok_actual_cost
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)

# glm-5.3-flash on the general API plan is declared at 0.15 in / 0.50 out USD per 1M tokens:
# (231 * 0.15 + 277 * 0.50) / 1_000_000.
_DECLARED_COST_USD = 0.00017315


def _byok_payload(*, correlation_id: str, model: str) -> dict[str, object]:
    data = _real_delegation_completed_payload(
        correlation_id=correlation_id, tenant_id="beta-business-proof"
    )
    data.update(
        {
            "task_type": "summarization",
            "model_used": model,
            "prompt_tokens": 231,
            "completion_tokens": 277,
            "cumulative_attempt_cost": 0.0,
            "cost_tier_name": "tenant_overlay",
            "backend_id": "byok-glm-general",
        }
    )
    return data


@pytest.mark.integration
class TestByokPricedCostLandsInRealRow:
    async def test_declared_model_row_carries_cost_above_zero(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _byok_payload(correlation_id=correlation_id, model="glm-5.3-flash")
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT cost_usd, cost_measurement_source, cost_tier_name"
                " FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            assert row["cost_usd"] is not None
            assert float(row["cost_usd"]) == pytest.approx(_DECLARED_COST_USD)
            assert row["cost_measurement_source"] == "metered"
            assert row["cost_tier_name"] == "tenant_overlay"

    async def test_undeclared_model_row_is_stored_without_a_cost(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _byok_payload(
                correlation_id=correlation_id, model="glm-model-nobody-declared"
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT cost_usd, cost_measurement_source"
                " FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            assert row["cost_measurement_source"] == "no_cost_model"
            assert row["cost_usd"] is None or float(row["cost_usd"]) == 0.0

    async def test_a_model_only_models_dev_prices_lands_its_cost_in_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-23413 AC35: the projection prices from the lookup it passes the backends.

        The catalogue and the registry price nothing for this model, so the
        cost can only come from the models.dev lookup ``byok_actual_cost``
        passes in; a recorded price stands in for the live read.
        """
        monkeypatch.setattr(
            byok_actual_cost,
            "models_dev_price_with_read_time",
            lambda _provider, _model: (
                (Decimal("0.2"), Decimal("0.4")),
                "2026-11-22T00:00:00+00:00",
            ),
        )
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _byok_payload(
                correlation_id=correlation_id, model="glm-model-only-models-dev-prices"
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT cost_usd, cost_measurement_source"
                " FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            # (231 * 0.2 + 277 * 0.4) / 1_000_000
            assert float(row["cost_usd"]) == pytest.approx(0.000157)
            assert row["cost_measurement_source"] == "metered"

    async def test_the_priced_row_write_logs_the_source_that_priced_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OMN-26594: the write path that books the cost also names where the price came from.

        A reading of a running dev runtime holds this line to tell the catalogue leg of
        the price lookup from the models.dev leg; the row it priced lands beside it.
        """
        caplog.set_level(logging.INFO, logger=byok_actual_cost.logger.name)
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())
            data = _byok_payload(correlation_id=correlation_id, model="glm-5.3-flash")
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT cost_usd FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the write must have landed a real row"
            assert float(row["cost_usd"]) == pytest.approx(_DECLARED_COST_USD)
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.name == byok_actual_cost.logger.name
            and "BYOK call priced" in r.getMessage()
        ]
        assert any(
            "backend_id=byok-glm-general" in line
            and "model=glm-5.3-flash" in line
            and "price_source=byok_provider_backends" in line
            for line in lines
        ), lines
