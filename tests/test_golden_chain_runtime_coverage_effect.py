"""The effect returns broker-owned zero-membership evidence over the bus."""

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.runtime_coverage import (
    ModelRuntimeCoverageRequest,
    ModelRuntimeCoverageSnapshot,
)
from omnimarket.nodes.node_runtime_coverage_effect import HandlerRuntimeCoverageEffect

pytestmark = pytest.mark.unit


async def test_effect_command_to_snapshot_terminal(
    event_bus: EventBusInmemory, tmp_path
):
    root = tmp_path / "omnimarket" / "nodes"
    path = root / "node_github_repo_write_effect"
    path.mkdir(parents=True)
    (path / "contract.yaml").write_text(
        "name: node_github_repo_write_effect\nevent_bus:\n  subscribe_topics: [onex.cmd.omnimarket.github-repo-write-requested.v1]\n"
    )
    calls = []

    def read(config, prefixes):
        calls.append(prefixes)
        return (), ()

    handler = HandlerRuntimeCoverageEffect(
        read, lambda _lane: ("dev-broker", "local", {})
    )
    command = "onex.cmd.omnimarket.runtime-coverage-effect-requested.v1"
    terminal = "onex.evt.omnimarket.runtime-coverage-effect-completed.v1"

    async def on_command(message):
        request = ModelRuntimeCoverageRequest.model_validate_json(message.value)
        snapshot = await handler.handle(request)
        await event_bus.publish(
            terminal, key=None, value=snapshot.model_dump_json().encode()
        )

    request = ModelRuntimeCoverageRequest(
        bus_lane="dev", runtime_lanes=("runtime-a",), contract_roots=(str(root),)
    )
    await event_bus.start()
    try:
        await event_bus.subscribe(
            command, on_message=on_command, group_id="test-runtime-coverage-effect"
        )
        await event_bus.publish(
            command, key=None, value=request.model_dump_json().encode()
        )
        history = await event_bus.get_event_history(topic=terminal)
        assert len(history) == 1
        assert (
            history[0].topic
            == "onex.evt.omnimarket.runtime-coverage-effect-completed.v1"
        )
        snapshot = ModelRuntimeCoverageSnapshot.model_validate_json(history[0].value)
        assert snapshot.error == ""
        assert snapshot.members == ()
        assert snapshot.commands[0].node == "node_github_repo_write_effect"
        assert len(calls) == 1
    finally:
        await event_bus.close()
