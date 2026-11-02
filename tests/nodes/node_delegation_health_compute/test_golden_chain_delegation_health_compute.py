# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21927: the recorded false-green counts through the registered contract on the in-memory bus.

Golden chain: the counts of 2026-11-02T00:01Z read DEGRADED. Error chain: a request with a blank
correlation id is refused with no result.
"""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_delegation_health_compute as node_package
from omnimarket.nodes.node_delegation_health_compute.models.model_delegation_health import (
    EnumDelegationHealthVerdict,
    ModelDelegationHealthVerdict,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
FIXTURE = Path(__file__).parent / "fixtures" / "recorded_2026_11_02T0001Z.json"
COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-health-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-health-decided.v1"


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
    assert registered["node_delegation_health_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_reads_the_recorded_counts_degraded(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURE.read_text())["request"]
    runtime = _runtime(tmp_path, payload, "recorded")
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelDelegationHealthVerdict)
    assert result.verdict is EnumDelegationHealthVerdict.DEGRADED
    assert result.exit_code == 1
    assert result.correlation_id == "recorded-2026-11-02T0001Z"


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text())["request"]
    refused = _runtime(tmp_path, {**payload, "correlation_id": ""}, "bad")
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
