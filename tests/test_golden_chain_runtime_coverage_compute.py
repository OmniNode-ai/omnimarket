"""The compute node emits a FAIL coverage result over the in-memory bus."""

from datetime import UTC, datetime

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.runtime_coverage import (
    ModelCommandSubscription,
    ModelRuntimeCoverageSnapshot,
)
from omnimarket.nodes.node_runtime_coverage_compute import HandlerRuntimeCoverageCompute

pytestmark = pytest.mark.unit


async def test_compute_command_to_coverage_terminal(event_bus: EventBusInmemory):
    handler = HandlerRuntimeCoverageCompute()
    command = "onex.cmd.omnimarket.runtime-coverage-compute-requested.v1"
    terminal = "onex.evt.omnimarket.runtime-coverage-compute-completed.v1"

    async def on_command(message):
        request = ModelRuntimeCoverageSnapshot.model_validate_json(message.value)
        result = handler.handle(request)
        await event_bus.publish(
            terminal, key=None, value=result.model_dump_json().encode()
        )

    request = ModelRuntimeCoverageSnapshot(
        bus_lane="dev",
        broker="dev-broker",
        observed_at=datetime.now(UTC),
        commands=(
            ModelCommandSubscription(
                node="node_github_repo_write_effect",
                topic="onex.cmd.omnimarket.github-repo-write-requested.v1",
            ),
        ),
    )
    await event_bus.start()
    try:
        await event_bus.subscribe(
            command, on_message=on_command, group_id="test-runtime-coverage-compute"
        )
        await event_bus.publish(
            command, key=None, value=request.model_dump_json().encode()
        )
        history = await event_bus.get_event_history(topic=terminal)
        assert len(history) == 1
        assert (
            history[0].topic
            == "onex.evt.omnimarket.runtime-coverage-compute-completed.v1"
        )
        from omnimarket.models.runtime_coverage import ModelRuntimeCoverageResult

        result = ModelRuntimeCoverageResult.model_validate_json(history[0].value)
        assert result.status == "FAIL"
        assert result.commands[0].serving_runtimes == 0
    finally:
        await event_bus.close()
