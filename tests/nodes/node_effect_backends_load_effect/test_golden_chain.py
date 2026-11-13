# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect backends load: a typed command through the in-memory runtime (OMN-23932)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_effect_backends_load_effect.models.model_effect_backends_load_request import (
    ModelEffectBackendsLoadRequest,
)
from omnimarket.nodes.node_effect_backends_load_effect.models.model_loaded_contracts import (
    ModelLoadedContracts,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_effect_backends_load_effect")))
    / "contract.yaml"
)
NODE_NAME = "node_x_effect"


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
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_golden_chain_publishes_the_load_completed_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.effect-backends-load-completed.v1"
    ]


@pytest.mark.asyncio
async def test_golden_chain_reads_one_node_contract(tmp_path: Path) -> None:
    folder = tmp_path / NODE_NAME
    folder.mkdir()
    (folder / "contract.yaml").write_text(
        yaml.safe_dump({"node_name": NODE_NAME, "node_type": "effect"}),
        encoding="utf-8",
    )
    request = ModelEffectBackendsLoadRequest(root=str(tmp_path))
    runtime, _status = await _run(tmp_path, request.model_dump(mode="json"))
    result = runtime.handler_result
    assert isinstance(result, ModelLoadedContracts)
    assert len(result.documents) == 1
    assert result.root_unavailable == ""


@pytest.mark.asyncio
async def test_error_chain_reports_an_absent_root(tmp_path: Path) -> None:
    request = ModelEffectBackendsLoadRequest(root=str(tmp_path / "absent"))
    runtime, _status = await _run(tmp_path, request.model_dump(mode="json"))
    result = runtime.handler_result
    assert isinstance(result, ModelLoadedContracts)
    assert result.root_unavailable != ""
    assert result.documents == ()


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path: Path) -> None:
    runtime, status = await _run(tmp_path, {"root": str(tmp_path), "unexpected": True})
    assert status is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
