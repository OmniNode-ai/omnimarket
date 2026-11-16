# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1 on the real Postgres write path: a refused run stores no saving.

The delegation projection derives each row's modelled_savings_usd while it
writes (modelled_run_savings_columns, the projection write-path seam). A run
whose answer the quality gate refused saved nothing, so its stored saving is 0,
while a run that passed with the same tokens and cost stores a positive one.

Real Postgres, never a mock: the column is NUMERIC and only a real connection
enforces how the derived Decimal binds. Runs against the INTEGRATION_POSTGRES_*
database, in a disposable schema with the live migrations (the OMN-15909
provisioning), and skips when that database is not configured.

Failure modes, each with a test below:
  F1  a refused terminal stores a positive modelled saving
  F2  the fix stores no saving at all for a run that passed (a vacuous zero)
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)


@pytest.mark.integration
class TestRefusedRunSavingOnRealPostgres:
    async def test_a_refused_terminal_stores_zero_and_a_passed_one_a_saving(
        self,
    ) -> None:
        """F1, F2."""
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            written: dict[str, str] = {}
            for label, extra in (
                ("passed", {}),
                (
                    "refused",
                    {
                        "quality_passed": False,
                        "terminal_failure_cause": "quality_gate_refused",
                    },
                ),
            ):
                correlation_id = str(uuid4())
                data = {
                    **_real_delegation_completed_payload(
                        correlation_id=correlation_id, tenant_id="beta-business-proof"
                    ),
                    **extra,
                }
                meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)
                ok = await runner.project_event(
                    runner._topic_delegation_completed, data, meta
                )
                assert ok is True
                written[label] = correlation_id

            rows = {
                label: await admin_conn.fetchrow(
                    "SELECT modelled_savings_usd, terminal_failure_cause"
                    " FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
                for label, correlation_id in written.items()
            }
            assert rows["refused"] is not None
            assert rows["passed"] is not None
            assert rows["refused"]["terminal_failure_cause"] == "quality_gate_refused"
            assert rows["passed"]["modelled_savings_usd"] is not None
            assert Decimal(str(rows["passed"]["modelled_savings_usd"])) > Decimal("0")
            assert Decimal(str(rows["refused"]["modelled_savings_usd"])) == Decimal("0")
