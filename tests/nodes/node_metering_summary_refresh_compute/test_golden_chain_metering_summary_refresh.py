# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 golden chain: delegation terminal -> refresh command -> stored rows.

The producer's output crosses the bus as JSON on the contract's terminal
topic, and the metering projection's writer consumes that topic: the tenant's
runs are read under its own context and folded into day and all-time rows.
The store is the in-memory fake of the store refresh tests, not Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.nodes.node_metering_summary_refresh_compute import (
    HandlerMeteringSummaryRefreshOnTerminal,
    ModelDelegationTerminalRefreshTrigger,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)
from tests.nodes.node_projection_metering_summary.test_omn17427_store_refresh import (
    FakeStore,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_REFRESH_COMMAND = "onex.cmd.omnimarket.metering-summary-refresh-requested.v1"


def _run(correlation: str, at: datetime) -> dict[str, Any]:
    return {
        "correlation_id": correlation,
        "created_at": at,
        "delegated_to": "qwen3.8-27b",
        "model_name": "qwen3.8-27b",
        "task_type": "document",
        "tokens_input": 120,
        "tokens_output": 30,
        "cost_usd": None,
        "cost_savings_usd": 0,
    }


def test_terminal_to_command_to_stored_rows() -> None:
    contract = yaml.safe_load(
        (_NODES / "node_metering_summary_refresh_compute/contract.yaml").read_text()
    )
    assert contract["terminal_event"] == _REFRESH_COMMAND

    finished = datetime(2026, 10, 19, 8, 2, tzinfo=UTC)
    store = FakeStore(
        {_TENANT: [_run("r1", finished - timedelta(days=1)), _run("r2", finished)]}
    )
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = cast(Any, store)
    writer._standalone_bindings = None

    command = HandlerMeteringSummaryRefreshOnTerminal().handle(
        ModelDelegationTerminalRefreshTrigger.model_validate(
            {
                "correlation_id": "r2",
                "tenant_id": _TENANT,
                "emitted_at": finished.isoformat(),
            }
        )
    )
    assert command is not None
    wire = json.loads(command.model_dump_json())
    applied = writer.handle({**wire, "_topic": contract["terminal_event"]})

    assert applied["tenants_refreshed"] == [_TENANT]
    assert applied["reason"] == "delegation_terminal"
    stored = sorted((k[1], k[2]) for k in store.rows if k[0] == _TENANT)
    # The tenant had no all row yet, so the first refresh folded it whole.
    assert stored == [("all", ""), ("day", "2026-10-18"), ("day", "2026-10-19")]
    assert len(command.model_dump_json()) < 1024
