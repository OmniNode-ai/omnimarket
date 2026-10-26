# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository reference gate: typed command through the in-memory runtime."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_retired_model_reference_check_effect.models.model_retired_model_reference_check import (
    ModelRetiredModelReferenceCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_retired_model_reference_check_effect")))
    / "contract.yaml"
)


def test_golden_chain_declares_the_terminal_topic():
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.retired-model-reference-check-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_golden_chain_typed_request_returns_the_reference_verdict(
    tmp_path, refused
):
    source = tmp_path / "history.md"
    source.write_text("gpt" + "-oss: history\n" if refused else "deployment-owned\n")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"repo_root": str(tmp_path), "paths": [source.name]}))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelRetiredModelReferenceCheckResult)
    assert result.admitted is (not refused)
    assert result.exit_code == int(refused)
    assert result.output_lines == (
        ("history.md:1: retired-model reference",) if refused else ()
    )


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
