# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> bus -> effect, a dry run that sends and logs nothing."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_awaiting_reply_effect as node_package
from omnimarket.nodes.node_awaiting_reply_effect.models.model_awaiting_reply_send import (
    EnumSendOutcome,
    ModelAwaitingReplySendRequest,
    ModelAwaitingReplySendResult,
)
from tests.nodes.node_awaiting_reply_compute.test_awaiting_reply_compute import item
from tests.nodes.node_awaiting_reply_effect.test_awaiting_reply_effect import (
    request,
    waiting,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def test_golden_chain_awaiting_reply_effect_is_a_registered_entry_point() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_awaiting_reply_effect"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_awaiting_reply_effect_dry_run_over_the_bus(
    tmp_path: Path,
) -> None:
    send: ModelAwaitingReplySendRequest = request(waiting(item()), dry_run=True)
    send = send.model_copy(update={"correlation_id": uuid4()})
    input_path = tmp_path / "request.json"
    input_path.write_text(send.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert isinstance(result, ModelAwaitingReplySendResult)
    assert result.records[0].outcome is EnumSendOutcome.DRY_RUN
    assert result.sent_count == 0
