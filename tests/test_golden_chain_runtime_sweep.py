"""Golden chain test for node_runtime_sweep.

Verifies the handler can check node descriptions, handler wiring,
topic symmetry, and emit completion events via EventBusInmemory.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    EnumFindingType,
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

CMD_TOPIC = "onex.cmd.omnimarket.runtime-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.runtime-sweep-completed.v1"


@pytest.mark.unit
class TestRuntimeSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_clean_contracts(self, event_bus: EventBusInmemory) -> None:
        """Contracts with real descriptions and wired handlers should be clean."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_test",
                    description="A proper node description for testing purposes",
                    handler_module="omnimarket.handlers.test",
                    handler_exists=True,
                    publish_topics=["onex.evt.test.done.v1"],
                    subscribe_topics=["onex.cmd.test.start.v1"],
                )
            ],
            topic_producers=["onex.cmd.test.start.v1"],
            topic_consumers=["onex.evt.test.done.v1"],
        )
        result = handler.handle(request)

        assert result.status == "clean"
        assert result.total_findings == 0

    async def test_placeholder_description(self, event_bus: EventBusInmemory) -> None:
        """Placeholder descriptions should be flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_bad",
                    description="compute+abc123",
                )
            ]
        )
        result = handler.handle(request)

        assert result.status == "findings"
        assert result.by_type.get("PLACEHOLDER_DESCRIPTION", 0) >= 1

    async def test_missing_description(self, event_bus: EventBusInmemory) -> None:
        """Empty descriptions should be flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_empty",
                    description="",
                )
            ]
        )
        result = handler.handle(request)

        assert result.by_type.get("MISSING_DESCRIPTION", 0) >= 1

    async def test_unwired_handler(self, event_bus: EventBusInmemory) -> None:
        """Handler declared but not found should be flagged as CRITICAL."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_ghost",
                    description="A real description for the ghost node handler",
                    handler_module="omnimarket.handlers.ghost",
                    handler_exists=False,
                )
            ]
        )
        result = handler.handle(request)

        assert result.by_type.get("UNWIRED_HANDLER", 0) >= 1
        unwired = [
            f
            for f in result.findings
            if f.finding_type == EnumFindingType.UNWIRED_HANDLER
        ]
        assert unwired[0].severity == "CRITICAL"

    async def test_producer_only_topic(self, event_bus: EventBusInmemory) -> None:
        """Topics with producer but no consumer should be flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_pub",
                    description="A publishing node with real description text",
                    publish_topics=["onex.evt.orphan.topic.v1"],
                )
            ],
            topic_consumers=[],
        )
        result = handler.handle(request)

        assert result.by_type.get("PRODUCER_ONLY", 0) >= 1

    async def test_consumer_only_topic(self, event_bus: EventBusInmemory) -> None:
        """Topics with consumer but no producer should be flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_sub",
                    description="A subscribing node with real description text",
                    subscribe_topics=["onex.cmd.orphan.topic.v1"],
                )
            ],
            topic_producers=[],
        )
        result = handler.handle(request)

        assert result.by_type.get("CONSUMER_ONLY", 0) >= 1

    async def test_symmetric_topics(self, event_bus: EventBusInmemory) -> None:
        """Topics with both producer and consumer should not be flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_sym",
                    description="A symmetric node with real description for test",
                    publish_topics=["onex.evt.sym.done.v1"],
                    subscribe_topics=["onex.cmd.sym.start.v1"],
                )
            ],
            topic_producers=["onex.cmd.sym.start.v1"],
            topic_consumers=["onex.evt.sym.done.v1"],
        )
        result = handler.handle(request)

        symmetry_findings = [
            f
            for f in result.findings
            if f.finding_type
            in (EnumFindingType.PRODUCER_ONLY, EnumFindingType.CONSUMER_ONLY)
        ]
        assert len(symmetry_findings) == 0

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeRuntimeSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            contracts = [ModelContractInput(**c) for c in payload.get("contracts", [])]
            request = RuntimeSweepRequest(contracts=contracts)
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "total_findings": result.total_findings,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-runtime"
        )

        cmd_payload = json.dumps(
            {
                "contracts": [
                    {
                        "node_name": "node_ok",
                        "description": "A properly described test node for runtime sweep",
                    }
                ]
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_dry_run_flag(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(contracts=[], dry_run=True)
        result = handler.handle(request)

        assert result.dry_run is True

    async def test_short_description_flagged(self, event_bus: EventBusInmemory) -> None:
        """Descriptions shorter than 10 chars should be flagged as placeholder."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_short",
                    description="TBD",
                )
            ]
        )
        result = handler.handle(request)

        assert result.by_type.get("PLACEHOLDER_DESCRIPTION", 0) >= 1
