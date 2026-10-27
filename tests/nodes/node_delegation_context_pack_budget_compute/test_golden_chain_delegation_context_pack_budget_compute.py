# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21119: context-pack budget admission through the registered bus contract.

Set OMN21119_TEST_BUS=kafka to run these cases on a separately configured Kafka binding;
the default is the in-memory binding. A missing Kafka configuration fails the cases.

Golden chain: a profile-naming task class with room under the ceiling is told to request a pack
with the room as its budget. Error chain: a request with no correlation id fails with no result.
"""

from __future__ import annotations

import importlib
import json
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_delegation_context_pack_budget_compute as node_package
from omnimarket.nodes.node_delegation_context_pack_budget_compute.models.model_delegation_context_pack_budget import (
    EnumDelegationContextPackStatus,
    ModelDelegationContextPackBudgetRequest,
    ModelDelegationContextPackBudgetResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-context-pack-budget-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-context-pack-budget-decided.v1"
REQUEST: dict[str, Any] = {
    "correlation_id": "corr-21119-bus",
    "task_class": "fixture_delegation_payload",
    "context_profile": "delegation_payload_local",
    "profile_token_budget": 8000,
    "composed_request_tokens": 2500,
    "max_grounded_input_tokens": 8000,
    "token_estimator": "qwen3-tokenizer",
}


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeDelegationContextPackBudgetCompute, handler_type)
    for side, model in (
        ("input_model", ModelDelegationContextPackBudgetRequest),
        ("output_model", ModelDelegationContextPackBudgetResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert (
        registered["node_delegation_context_pack_budget_compute"].load() is node_package
    )


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    (tmp_path / name).mkdir()
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={
            "event_bus": os.environ.get("OMN21119_TEST_BUS", "inmemory")
        },
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_requests_a_pack_with_the_room_left(
    tmp_path: Path,
) -> None:
    runtime = await _run(tmp_path, REQUEST, "request")
    result = runtime.handler_result
    assert isinstance(result, ModelDelegationContextPackBudgetResult)
    assert result.status is EnumDelegationContextPackStatus.REQUEST_PACK
    assert result.token_budget_override == 5500
    assert result.dispatch_allowed is False
    assert result.correlation_id == "corr-21119-bus"
    assert (
        ModelDelegationContextPackBudgetResult.model_validate_json(
            result.model_dump_json()
        )
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    bad = {**REQUEST, "correlation_id": ""}
    input_path = tmp_path / "bad.json"
    input_path.write_text(json.dumps(bad))
    (tmp_path / "bad").mkdir()
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "bad",
        backend_overrides={
            "event_bus": os.environ.get("OMN21119_TEST_BUS", "inmemory")
        },
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("measurement", "expected"),
    [
        (None, "refuse_unmeasured_input"),
        (8000, "attach"),
        (8001, "refuse_input_overflow"),
    ],
)
async def test_bus_admission_requires_a_measured_input_within_the_ceiling(
    tmp_path: Path, measurement: int | None, expected: str
) -> None:
    runtime = await _run(
        tmp_path,
        {
            **REQUEST,
            "context_pack_hash": "sha256:" + "f" * 64,
            "assembled_request_tokens": measurement,
        },
        expected,
    )
    result = runtime.handler_result
    assert isinstance(result, ModelDelegationContextPackBudgetResult)
    assert result.status.value == expected
    assert result.dispatch_allowed is (expected == "attach")
    assert result.assembled_request_tokens == measurement
    assert bool(result.context_pack_hash) is (expected == "attach")
    assert (
        ModelDelegationContextPackBudgetResult.model_validate_json(
            result.model_dump_json()
        )
        == result
    )
