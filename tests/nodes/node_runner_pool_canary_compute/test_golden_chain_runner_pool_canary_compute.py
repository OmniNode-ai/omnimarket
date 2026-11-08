# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22945: a canary round through the registered contract on the in-memory bus.

Golden chain: seven exit-0 canaries with every runner online read all WORKING. Error chain: a
request with a blank correlation id is refused with no result.
"""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_runner_pool_canary_compute as node_package
from omnimarket.nodes.node_runner_pool_canary_compute.models.model_runner_pool_canary import (
    EnumRunnerPoolCanaryState,
    ModelRunnerPoolCanaryVerdict,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.runner-pool-canary-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.runner-pool-canary-decided.v1"
RUNNERS = {
    "omninode-runner": "omninode-runner-3",
    "omninode-verify": "omninode-verify-runner-1",
    "customer-plane": "omninode-customer-plane-runner-2",
    "hp051-ci": "hp051-ci-runner-1",
    "hp051-ci-mq": "hp051-ci-mq-runner-2",
    "hp053-ci": "hp053-ci-runner-4",
    "mini": "omninode-mini-runner-1",
}


def _payload(correlation_id: str = "r0-golden") -> dict[str, object]:
    return {
        "correlation_id": correlation_id,
        "observations": [
            {"pool": p, "job_conclusion": "success", "runner_name": r, "exit_code": 0}
            for p, r in RUNNERS.items()
        ],
        "online_by_pool": dict.fromkeys(RUNNERS, 2),
        "registered_by_pool": dict.fromkeys(RUNNERS, 2),
    }


def _runtime(tmp_path: Path, payload: dict[str, object], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    (tmp_path / name).mkdir()
    return RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )


def test_contract_declares_its_command_and_terminal_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC


def test_entry_point_is_registered() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_runner_pool_canary_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_reads_all_seven_pools_working(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _payload(), "golden")
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelRunnerPoolCanaryVerdict)
    assert result.all_working is True
    assert {v.state for v in result.verdicts} == {EnumRunnerPoolCanaryState.WORKING}
    assert result.correlation_id == "r0-golden"


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    refused = _runtime(tmp_path, _payload(""), "bad")
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
