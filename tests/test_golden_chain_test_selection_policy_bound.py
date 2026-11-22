# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the policy-bound operation over the in-memory bus."""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
    SourceTestFilesInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_policy_bound import (
    EnumBoundMergeKind,
    ModelBoundMerge,
    ModelPolicyBoundRequest,
    ModelPolicyBoundResult,
    PolicyBoundInputError,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorFailed,
)

pytestmark = pytest.mark.unit

DURATIONS = {"tests/a/test_x.py": 200.0, "tests/a/test_y.py": 50.0}


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_test_selection_selector_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return document


def _payload() -> bytes:
    return (
        ModelPolicyBoundRequest(
            repo_root="/unused",
            merges=(
                ModelBoundMerge(
                    sha="a" * 40,
                    selected_paths=("tests/a/",),
                    kind=EnumBoundMergeKind.NARROWED,
                ),
            ),
            shard_setup_seconds=5.0,
            collection_gate_seconds=2.0,
        )
        .model_dump_json()
        .encode()
    )


async def _run_chain(
    durations: dict[str, float] | None, payload: bytes
) -> list[tuple[str, bytes]]:
    contract = _contract()
    routes = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    spec = routes["calculate_policy_bound"]["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["name"])(
        SourceInMemory(durations),
        SourceTestFilesInMemory(tuple(DURATIONS)),
        load_policy(),
    )
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    failed_topic = contract["runtime_dispatch"]["terminal_events"]["failure"]
    bus = EventBusInmemory()
    seen: list[tuple[str, bytes]] = []
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handle(message: Any) -> None:
        try:
            request = ModelPolicyBoundRequest.model_validate_json(message.value)
            result = handler.handle(request)
        except (ValidationError, PolicyBoundInputError) as exc:
            failure = ModelSelectorFailed(reason=type(exc).__name__)
            await bus.publish(
                failed_topic, key=None, value=failure.model_dump_json().encode()
            )
            return
        await bus.publish(
            terminal_topic, key=None, value=result.model_dump_json().encode()
        )

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, message.value))
            if not done.done():
                done.set_result(None)

        return collect

    await bus.start()
    try:
        await bus.subscribe(request_topic, on_message=handle, group_id="pb")
        for topic in (terminal_topic, failed_topic):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"pb-{topic}"
            )
        await bus.publish(request_topic, key=None, value=payload)
        await asyncio.wait_for(done, timeout=5)
    finally:
        await bus.close()
    return seen


@pytest.mark.asyncio
async def test_golden_chain_policy_bound_ends_in_the_terminal_event() -> None:
    seen = await _run_chain(DURATIONS, _payload())

    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    result = ModelPolicyBoundResult.model_validate_json(seen[0][1])
    assert result.suite_total_seconds == 250.0
    assert result.merge_costs[0].selected_seconds == 250.0
    assert result.cost_columns_measured is True


@pytest.mark.asyncio
async def test_error_chain_policy_bound_unreadable_durations_publish_the_failure() -> (
    None
):
    seen = await _run_chain(None, _payload())

    failed_topic = _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    assert [t for t, _ in seen] == [failed_topic]
    failure = ModelSelectorFailed.model_validate_json(seen[0][1])
    assert failure.reason == "PolicyBoundInputError"


@pytest.mark.asyncio
async def test_error_chain_policy_bound_bad_payload_publishes_the_failure() -> None:
    seen = await _run_chain(DURATIONS, b'{"repo_root": "/x", "merges": []}')

    failed_topic = _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    assert [t for t, _ in seen] == [failed_topic]
    assert (
        ModelSelectorFailed.model_validate_json(seen[0][1]).reason == "ValidationError"
    )
