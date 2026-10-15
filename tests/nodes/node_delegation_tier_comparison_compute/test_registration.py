# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered tier comparison node resolves to the typed compute handler."""

from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import (
    LocalRuntimeDispatch,
    _resolve_node_route,
)
from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    default_command_topic,
    default_response_topic,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_tier import (
    EnumComparisonTier,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_record import (
    ModelComparisonRecord,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_request import (
    ModelComparisonRequest,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_result import (
    ModelComparisonResult,
)
from omnimarket.nodes.node_delegation_tier_comparison_compute.handlers.handler_delegation_tier_comparison import (
    HandlerDelegationTierComparison,
)
from tests.nodes.node_delegation_tier_comparison_compute.support import (
    claude_arm,
    local_arm,
    request,
)

pytestmark = pytest.mark.unit


def test_registered_tier_comparison_route() -> None:
    route = _resolve_node_route("node_delegation_tier_comparison_compute")
    assert route.command_topic == (
        "onex.cmd.omnimarket.delegation-tier-comparison-requested.v1"
    )
    assert route.terminal_topic == (
        "onex.evt.omnimarket.delegation-tier-comparison-measured.v1"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationTierComparison
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelComparisonRequest
    )


@pytest.mark.asyncio
async def test_runtime_terminal_keeps_required_nullable_record_fields(
    tmp_path: Path,
) -> None:
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(),
        state_root=tmp_path,
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="node_delegation_tier_comparison_compute",
            requester="tier-comparison-test",
            payload=request(
                claude_arm(EnumComparisonTier.SONNET, 54), local_arm(50)
            ).model_dump(mode="json"),
            correlation_id=uuid4(),
            response_topic=default_response_topic(),
            timeout_seconds=10,
        )
    )

    assert terminal.status == "completed", terminal.error_message
    assert evidence.runtime_observation.status == "OBSERVED"
    assert terminal.payload is not None
    result = ModelComparisonResult.model_validate(terminal.payload)
    assert len(result.records) == 2
    for record in terminal.payload["records"]:
        assert set(record) == set(ModelComparisonRecord.model_fields)
    assert terminal.payload["records"][0]["arm_host"] is None
    assert terminal.payload["records"][0]["saving_per_task_usd"] is None
