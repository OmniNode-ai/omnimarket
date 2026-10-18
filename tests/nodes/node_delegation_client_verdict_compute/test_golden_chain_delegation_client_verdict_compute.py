# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21098: the client verdict request through the registered contract on the in-memory bus.

Golden chain: an accepted, identity-matched terminal in the required column answers. Error chain:
a request with no correlation id is refused with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_delegation_client_verdict_compute as node_package
from omnimarket.models.claude_code_binding import EnumClientVerdictAction
from omnimarket.nodes.node_delegation_client_verdict_compute.models.model_delegation_client_verdict import (
    EnumDelegationClientVerdictRow,
    ModelDelegationClientVerdictRequest,
    ModelDelegationClientVerdictResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-client-verdict-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-client-verdict-decided.v1"
ACCEPTED: dict[str, Any] = {
    "correlation_id": "corr-21098",
    "client_max_mode": "required",
    "effective_mode": "required",
    "outcome": "terminal",
    "task_type_allowlisted": True,
    "terminal_status": "completed",
    "terminal_quality_gate_passed": True,
    "client_call_key": "key-21098",
    "request_fingerprint": "fp-21098",
    "echoed_client_call_key": "key-21098",
    "echoed_request_fingerprint": "fp-21098",
}


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeDelegationClientVerdictCompute, handler_type)
    for side, model in (
        ("input_model", ModelDelegationClientVerdictRequest),
        ("output_model", ModelDelegationClientVerdictResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_delegation_client_verdict_compute"].load() is node_package


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_answers_an_accepted_terminal(
    tmp_path: Path,
) -> None:
    runtime = await _run(tmp_path, ACCEPTED, "accepted")
    result = runtime.handler_result
    assert isinstance(result, ModelDelegationClientVerdictResult)
    assert result.action == EnumClientVerdictAction.ANSWER
    assert result.row == EnumDelegationClientVerdictRow.TERMINAL_ACCEPTED
    assert result.correlation_id == "corr-21098"
    assert (
        ModelDelegationClientVerdictResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    bad = {**ACCEPTED, "correlation_id": ""}
    input_path = tmp_path / "bad.json"
    input_path.write_text(json.dumps(bad))
    (tmp_path / "bad").mkdir()
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
