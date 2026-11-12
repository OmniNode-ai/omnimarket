# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect backends check: a typed command through the in-memory runtime (OMN-23932)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_effect_backends_check_compute.models.model_effect_backends_report import (
    ModelEffectBackendsReport,
)
from omnimarket.nodes.node_effect_backends_check_compute.models.model_effect_backends_request import (
    ModelEffectBackendsRequest,
)
from omnimarket.nodes.node_effect_backends_check_compute.models.model_effect_contract_document import (
    ModelEffectContractDocument,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_effect_backends_check_compute")))
    / "contract.yaml"
)
NODE_NAME = "node_a_effect"


async def _run(
    tmp_path: Path, request_body: dict[str, object]
) -> tuple[RuntimeLocal, EnumWorkflowResult]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_body))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    status = await runtime.run_async()
    return runtime, status


def test_golden_chain_declares_the_command_and_terminal_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    for topic in contract["runtime_dispatch"]["terminal_events"].values():
        assert topic in contract["event_bus"]["publish_topics"]


def test_golden_chain_publishes_the_check_completed_and_failed_events() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.effect-backends-check-completed.v1",
        "onex.evt.omnimarket.effect-backends-check-failed.v1",
    ]


@pytest.mark.asyncio
async def test_golden_chain_lists_an_effect_node_without_backends(
    tmp_path: Path,
) -> None:
    request = ModelEffectBackendsRequest(
        contracts=(
            ModelEffectContractDocument(
                path=f"{NODE_NAME}/contract.yaml",
                document={"node_name": NODE_NAME, "node_type": "effect"},
            ),
        )
    )
    runtime, _status = await _run(tmp_path, request.model_dump(mode="json"))
    result = runtime.handler_result
    assert isinstance(result, ModelEffectBackendsReport)
    assert result.without_in_memory_default == (NODE_NAME,)
    assert result.not_on_both == (NODE_NAME,)
    assert result.effects_total == 1
    assert result.entries[0].path == f"{NODE_NAME}/contract.yaml"


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path: Path) -> None:
    runtime, status = await _run(tmp_path, {"contracts": [], "unexpected": True})
    assert status is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
