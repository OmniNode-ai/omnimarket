# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handwritten event-payload gate: typed command through the in-memory runtime."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_handwritten_event_payload_check_effect.models.model_handwritten_event_payload_check import (
    ModelHandwrittenEventPayloadCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_handwritten_event_payload_check_effect")))
    / "contract.yaml"
)


def test_golden_chain_declares_the_terminal_topic():
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.handwritten-event-payload-check-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


@pytest.mark.asyncio
async def test_golden_chain_typed_request_refuses_a_tree_below_the_scan_floor(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"repo_root": str(tmp_path)}))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelHandwrittenEventPayloadCheckResult)
    assert result.admitted is False
    assert result.exit_code == 1
    assert "scan floor" in result.output_lines[0]


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"repo_root": str(tmp_path), "unexpected": True}))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
