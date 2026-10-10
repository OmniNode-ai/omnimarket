# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: the usage-by-model-day writer starts when run as a module.

The catalog's laptop bundle runs each projection writer as
``python -m <handler module>`` (the llm-cost writer, the delegation writer).
Without an entry point the module imports and exits, so a container running
it would never consume a message. These tests run the module exactly as
``python -m`` does, with only the runner loop stubbed.
"""

from __future__ import annotations

import asyncio
import runpy
import sys
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    _INSERT_CALL,
    _RECOUNT_AGGREGATE,
)
from omnimarket.projection.runner import BaseProjectionRunner
from tests.test_omn19978_usage_by_model_day import CALLS, _event
from tests.test_omn19978_usage_by_model_day_real_postgres import (
    _MIGRATIONS,
    _connect_or_skip,
)

MODULE = (
    "omnimarket.nodes.node_projection_usage_by_model_day.handlers."
    "handler_usage_by_model_day_writer"
)


def _run_module(run_name: str) -> AsyncMock:
    # autospec keeps run() a bound method, so the mock records the runner as self.
    saved = sys.modules.pop(MODULE, None)
    try:
        with patch.object(BaseProjectionRunner, "run", autospec=True) as run:
            runpy.run_module(MODULE, run_name=run_name)
    finally:
        if saved is not None:
            sys.modules[MODULE] = saved
    return run


def test_running_the_module_starts_the_usage_writer() -> None:
    run = _run_module("__main__")

    run.assert_awaited_once()
    assert run.await_args is not None
    runner = run.await_args.args[0]
    assert type(runner).__name__ == "UsageByModelDayProjectionWriter"


def test_the_started_writer_consumes_its_contract_topics() -> None:
    run = _run_module("__main__")

    assert run.await_args is not None, "the module started no writer"
    runner = run.await_args.args[0]
    assert runner.topics, "the writer would subscribe to nothing"
    assert runner.topics == runner.subscribe_topics


def test_importing_the_module_does_not_start_it() -> None:
    run = _run_module("not_main")

    run.assert_not_awaited()


_ENTRY_SCHEMA = "omn19972_usage_entrypoint_test"


@pytest.mark.integration
def test_real_postgres_the_writer_the_module_starts_writes_its_row() -> None:
    """The instance ``python -m`` builds folds a call that its own SQL stores.

    Needs INTEGRATION_POSTGRES_PASSWORD (and the other INTEGRATION_POSTGRES_*
    settings); skips without it. The writer's statements run in a scratch
    schema, as in the OMN-19978 real-Postgres proofs. The test is synchronous
    because running the module calls asyncio.run itself.
    """
    run = _run_module("__main__")
    assert run.await_args is not None, "the module started no writer"
    asyncio.run(_store_one_call(run.await_args.args[0]))


async def _store_one_call(writer: Any) -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_ENTRY_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_ENTRY_SCHEMA}")
        for migration in _MIGRATIONS:
            await conn.execute(
                migration.read_text(encoding="utf-8").replace(
                    "public.", f"{_ENTRY_SCHEMA}."
                )
            )
        delta = writer._derive.handle(_event(CALLS[0]))
        day = date.fromisoformat(delta.usage_day)
        inserted = await conn.fetch(
            _INSERT_CALL.replace("public.", f"{_ENTRY_SCHEMA}."),
            delta.call_id,
            delta.tenant_id,
            day,
            delta.model_id,
            delta.input_tokens,
            delta.output_tokens,
            delta.cost_usd,
            delta.occurred_at,
            delta.usage_source.value,
        )
        assert inserted, "the call was not stored"
        await conn.fetch(
            _RECOUNT_AGGREGATE.replace("public.", f"{_ENTRY_SCHEMA}."),
            delta.tenant_id,
            day,
            delta.model_id,
        )
        rows = await conn.fetch(f"SELECT * FROM {_ENTRY_SCHEMA}.usage_by_model_day")
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["model_id"] == delta.model_id
        assert rows[0]["input_tokens"] == delta.input_tokens
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_ENTRY_SCHEMA} CASCADE")
        await conn.close()
