# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run engine node bound to its contract: handler, models, topics, entry point, and a run over the bus."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_run_engine_effect as node_package
from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumRunEngineBackend,
    EnumRunStatus,
    EnumStartOutcome,
    ModelRunEngineOverlay,
    ModelRunEngineRequest,
    ModelRunEngineResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.run-engine-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.run-engine-completed.v1"


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_run_engine_contract_bound_handler_and_models() -> None:
    contract = _contract()
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerRunEngine
    assert issubclass(node_package.NodeRunEngineEffect, handler_type)
    module_name, _, class_name = contract["handler_routing"][
        "default_handler"
    ].partition(":")
    assert getattr(importlib.import_module(module_name), class_name) is handler_type
    for side, model in (
        ("input_model", ModelRunEngineRequest),
        ("output_model", ModelRunEngineResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_run_engine_effect"].load() is node_package


def test_run_engine_contract_bound_topics() -> None:
    contract = _contract()
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC


def test_run_engine_contract_overlay_declares_the_backend_key_and_its_values() -> None:
    overlay = _contract()["overlay"]
    assert overlay["key"] == "run_engine_backend"
    assert overlay["default"] == EnumRunEngineBackend.IN_MEMORY.value
    assert overlay["values"] == [member.value for member in EnumRunEngineBackend]
    assert ModelRunEngineOverlay().run_engine_backend.value == overlay["default"]


def test_run_engine_contract_bound_metadata_registers_the_node() -> None:
    metadata = dict(yaml.safe_load((NODE_DIR / "metadata.yaml").read_text()))
    assert metadata["name"] == "node_run_engine_effect"
    assert metadata["entry_points"]["onex.nodes"]["node_run_engine_effect"] == (
        "omnimarket.nodes.node_run_engine_effect"
    )


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_run_engine_contract_bound_golden_chain_over_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    started = await _run(
        tmp_path,
        {
            "operation": "start_run",
            "run": {
                "run_key": "golden-run-1",
                "steps": [
                    {"name": "hold", "kind": "wait_release", "condition": "gate"}
                ],
            },
        },
        "start",
    )
    result = started.handler_result
    assert isinstance(result, ModelRunEngineResult)
    assert result.backend == "in_memory"
    assert result.start_outcome is EnumStartOutcome.ACCEPTED
    read = await _run(
        tmp_path, {"operation": "get_run_state", "run_key": "golden-run-1"}, "read"
    )
    state = read.handler_result
    assert isinstance(state, ModelRunEngineResult)
    assert state.state is not None
    assert state.state.status in (EnumRunStatus.RUNNING, EnumRunStatus.WAITING)


@pytest.mark.asyncio
async def test_run_engine_contract_bound_error_chain_over_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    (tmp_path / "bad.json").write_text(json.dumps({"operation": "start_run"}))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=tmp_path / "bad.json",
        state_root=tmp_path / "state-bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
