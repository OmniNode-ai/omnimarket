# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> bus -> compute -> typed result, plus the refusal chain."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_awaiting_reply_compute as node_package
from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    EnumReplyClass,
    ModelAwaitingReplyRequest,
    ModelAwaitingReplyResult,
)
from tests.nodes.node_awaiting_reply_compute.test_awaiting_reply_compute import (
    bundle,
    item,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def test_golden_chain_awaiting_reply_compute_is_a_registered_entry_point() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_awaiting_reply_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_awaiting_reply_compute_decides_over_the_bus(
    tmp_path: Path,
) -> None:
    request = ModelAwaitingReplyRequest.model_validate(bundle(item()))
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert isinstance(result, ModelAwaitingReplyResult)
    assert result.waiting[0].reply_class is EnumReplyClass.AUTO
    assert (
        ModelAwaitingReplyResult.model_validate_json(result.model_dump_json()) == result
    )


@pytest.mark.asyncio
async def test_golden_chain_awaiting_reply_compute_refuses_a_malformed_bundle(
    tmp_path: Path,
) -> None:
    bad = bundle(item())
    bad["held"] = "reply held for review"
    input_path = tmp_path / "bad.json"
    input_path.write_text(json.dumps(bad))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
