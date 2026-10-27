# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer parity fails closed through both contract-driven local bus bindings."""

from __future__ import annotations

import asyncio
import importlib
import json
from itertools import product
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.event_bus.event_bus_semantic_fake import EventBusSemanticFake
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.nodes.node_context_replay_eval_compute.models import (
    EnumAnswerGrade,
    ModelAnswerComparisonRequest,
    ModelAnswerComparisonResult,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_replay_eval_compute/contract.yaml"
)


@pytest.mark.parametrize(
    "bus_class",
    [EventBusInmemory, EventBusSemanticFake],
    ids=["inmemory", "broker-semantics"],
)
@pytest.mark.parametrize(
    ("package", "native"),
    list(product(EnumAnswerGrade, repeat=2)),
)
async def test_answer_comparison_parity_on_bus_bindings(
    bus_class: type[EventBusInmemory] | type[EventBusSemanticFake],
    package: EnumAnswerGrade,
    native: EnumAnswerGrade,
) -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = next(
        route
        for route in manifest.contracts[0].handler_routing.handlers
        if route.operation == "compare_answers"
    )
    handler_type = getattr(
        importlib.import_module(route.handler.module), route.handler.name
    )
    dispatch = _make_dispatch_callback(handler_type(), event_model=route.event_model)
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    request = ModelAnswerComparisonRequest.model_validate(
        {
            "runs": [
                {
                    "run_id": run_id,
                    "cases": [
                        {
                            "case_id": "case-a",
                            "candidate_id": "candidate-a",
                            "package_answer": package,
                            "native_answer": native,
                        },
                        # Other successful answers cannot mask an unavailable arm.
                        {
                            "case_id": "case-b",
                            "candidate_id": "candidate-b",
                            "package_answer": "MATCH",
                            "native_answer": "MISMATCH",
                        },
                    ],
                }
                for run_id in ("r1", "r2")
            ]
        }
    )
    completed: asyncio.Future[ModelAnswerComparisonResult] = (
        asyncio.get_running_loop().create_future()
    )
    bus = bus_class()

    async def on_command(message: ModelEventMessage) -> None:
        result = await dispatch(
            {
                "payload": json.loads(message.value),
                "correlation_id": "00000000-0000-4000-8000-000000021432",
            }
        )
        assert result is not None
        await bus.publish(
            terminal_topic,
            key=None,
            value=ModelAnswerComparisonResult.model_validate(result.output_events[0])
            .model_dump_json()
            .encode(),
        )

    async def on_terminal(message: ModelEventMessage) -> None:
        completed.set_result(
            ModelAnswerComparisonResult.model_validate_json(message.value)
        )

    await bus.start()
    try:
        await bus.subscribe(
            terminal_topic, on_message=on_terminal, group_id="replay-result"
        )
        await bus.subscribe(
            command_topic, on_message=on_command, group_id="replay-command"
        )
        await bus.publish(
            command_topic, key=None, value=request.model_dump_json().encode()
        )
        result = await asyncio.wait_for(completed, timeout=5)
    finally:
        await bus.close()

    available = EnumAnswerGrade.UNAVAILABLE not in (package, native)
    # The second case makes the package's numeric match count at least native's
    # in every scenario, including MISMATCH/MATCH. Availability is still required.
    assert result.next_action_met is available
    assert all(run.package_at_least_native is available for run in result.runs)
    assert all(
        run.package_matches == 1 + int(package is EnumAnswerGrade.MATCH)
        and run.native_matches == int(native is EnumAnswerGrade.MATCH)
        and run.package_unavailable == int(package is EnumAnswerGrade.UNAVAILABLE)
        and run.native_unavailable == int(native is EnumAnswerGrade.UNAVAILABLE)
        for run in result.runs
    )
    assert result.candidate_ids_identical
    assert result.determinism_checked
    assert bool(result.falsifiers) is not available
