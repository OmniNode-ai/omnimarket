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

import runpy
import sys
from unittest.mock import AsyncMock, patch

from omnimarket.projection.runner import BaseProjectionRunner

MODULE = (
    "omnimarket.nodes.node_projection_usage_by_model_day.handlers."
    "handler_usage_by_model_day_writer"
)


def _run_as_main() -> AsyncMock:
    run = AsyncMock(return_value=None)
    saved = sys.modules.pop(MODULE, None)
    try:
        with patch.object(BaseProjectionRunner, "run", run):
            runpy.run_module(MODULE, run_name="__main__")
    finally:
        if saved is not None:
            sys.modules[MODULE] = saved
    return run


def test_running_the_module_starts_the_usage_writer() -> None:
    run = _run_as_main()

    run.assert_awaited_once()
    runner = run.await_args.args[0]
    assert type(runner).__name__ == "UsageByModelDayProjectionWriter"


def test_the_started_writer_consumes_its_contract_topics() -> None:
    run = _run_as_main()

    runner = run.await_args.args[0]
    assert runner.topics, "the writer would subscribe to nothing"
    assert runner.topics == runner.subscribe_topics


def test_importing_the_module_does_not_start_it() -> None:
    run = AsyncMock(return_value=None)
    saved = sys.modules.pop(MODULE, None)
    try:
        with patch.object(BaseProjectionRunner, "run", run):
            runpy.run_module(MODULE, run_name="not_main")
    finally:
        if saved is not None:
            sys.modules[MODULE] = saved

    run.assert_not_awaited()
