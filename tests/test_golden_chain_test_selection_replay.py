# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the selection replay of node_test_selection_selector_compute."""

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
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayFailed,
    ModelReplayRequest,
    ModelReplayResult,
)

pytestmark = pytest.mark.unit

TREE_ID = "0123456789abcdef0123456789abcdef01234567"


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_test_selection_selector_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return document


def _snapshot() -> dict[str, Any]:
    return {
        "tree_id": TREE_ID,
        "adjacency": {
            "schema_version": 1,
            "shared_modules": ["models"],
            "thresholds": {"modules_changed_for_full_suite": 6},
            "test_infrastructure_paths": ["pyproject.toml"],
            "adjacency": {
                "models": {"reverse_deps": []},
                "classifiers": {"reverse_deps": []},
            },
            "always_selected_paths": ["tests/gates/"],
        },
        "policy": load_policy().model_dump(mode="json"),
        "durations": {
            "tests/classifiers/test_c.py": 30.0,
            "tests/gates/test_g.py": 5.0,
        },
        "dirs": [
            "src/omnimarket",
            "src/omnimarket/classifiers",
            "tests",
            "tests/classifiers",
            "tests/gates",
        ],
        "files": {
            "src/omnimarket/classifiers/c.py": {},
            "tests/classifiers/test_c.py": {},
            "tests/gates/test_g.py": {},
        },
        "yaml_names": {},
    }


def _request(*merges: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "tree_id": TREE_ID,
        "snapshot": _snapshot(),
        "full_split_count": 20,
        "infrastructure_categories": {"pyproject.toml": "pyproject"},
        "merges": [{"sha": sha, "changed_files": files} for sha, files in merges],
    }


GOLDEN = _request(
    ("a" * 40, ["src/omnimarket/classifiers/c.py"]),
    ("b" * 40, ["pyproject.toml"]),
)


async def _run_chain(payload: bytes) -> list[tuple[str, bytes]]:
    """Publish ``payload`` on the contract's command topic; return every event the node published."""
    contract = _contract()
    operations = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    spec = operations["replay_selection"]["handler"]
    assert getattr(importlib.import_module(spec["module"]), spec["name"]) is (
        HandlerSelectionReplay
    )
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    failed_topic = contract["runtime_dispatch"]["terminal_events"]["failure"]
    assert request_topic in contract["event_bus"]["subscribe_topics"]
    assert {terminal_topic, failed_topic} <= set(
        contract["event_bus"]["publish_topics"]
    )

    handler = HandlerSelectionReplay()
    bus = EventBusInmemory()
    seen: list[tuple[str, bytes]] = []
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handle(message: Any) -> None:
        try:
            request = ModelReplayRequest.model_validate_json(message.value)
        except ValidationError as exc:
            failure = ModelReplayFailed(reason=type(exc).__name__)
            await bus.publish(
                failed_topic, key=None, value=failure.model_dump_json().encode()
            )
            return
        answer = handler.handle(request)
        topic = (
            failed_topic if isinstance(answer, ModelReplayFailed) else terminal_topic
        )
        await bus.publish(topic, key=None, value=answer.model_dump_json().encode())

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, message.value))
            if not done.done():
                done.set_result(None)

        return collect

    await bus.start()
    try:
        await bus.subscribe(request_topic, on_message=handle, group_id="tsr")
        for topic in (terminal_topic, failed_topic):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"r-{topic}"
            )
        await bus.publish(request_topic, key=None, value=payload)
        await asyncio.wait_for(done, timeout=10)
    finally:
        await bus.close()
    return seen


def test_contract_routes_replay_selection_with_its_typed_models() -> None:
    operations = {h["operation"]: h for h in _contract()["handler_routing"]["handlers"]}
    entry = operations["replay_selection"]
    assert entry["input_model"].endswith("model_selection_replay.ModelReplayRequest")
    assert entry["output_model"].endswith("model_selection_replay.ModelReplayResult")
    assert set(operations) >= {"select_test_shards", "resolve_test_paths"}


@pytest.mark.asyncio
async def test_golden_chain_replay_ends_in_the_terminal_event_with_every_decision() -> (
    None
):
    request = ModelReplayRequest.model_validate(GOLDEN)
    seen = await _run_chain(request.model_dump_json().encode())

    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    result = ModelReplayResult.model_validate_json(seen[0][1])
    assert [d.reason_code for d in result.decisions] == [
        "narrowed",
        "test_infrastructure",
    ]
    assert result.totals.narrowed == 1
    assert result.totals.full_suite == 1
    assert result.infrastructure_cross_tab.cells == {"pyproject": 1}
    assert result.decisions[0].selected_seconds == 35.0


@pytest.mark.asyncio
async def test_error_chain_replay_bad_payload_publishes_the_typed_failure_event() -> (
    None
):
    seen = await _run_chain(b'{"operation": "replay_selection", "merges": []}')

    failed_topic = _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    assert [t for t, _ in seen] == [failed_topic]
    failure = ModelReplayFailed.model_validate_json(seen[0][1])
    assert failure.reason == "ValidationError"
    assert failure.terminal_failure_cause == "ValidationError"


@pytest.mark.asyncio
async def test_error_chain_replay_unreadable_snapshot_publishes_the_typed_failure_event() -> (
    None
):
    request = ModelReplayRequest.model_validate(GOLDEN).model_copy(
        update={
            "snapshot": None,
            "snapshot_path": "config/does_not_exist.json.gz",
            "snapshot_sha256": "0" * 64,
        }
    )
    seen = await _run_chain(request.model_dump_json().encode())

    failed_topic = _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    assert [t for t, _ in seen] == [failed_topic]
    failure = ModelReplayFailed.model_validate_json(seen[0][1])
    assert failure.reason.startswith("snapshot unreadable")
    assert failure.terminal_failure_cause == failure.reason
