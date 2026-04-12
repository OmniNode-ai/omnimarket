"""Golden chain test for node_data_flow_sweep.

Verifies the handler can classify data flows by status and emit
completion events via EventBusInmemory.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    DataFlowSweepRequest,
    EnumFlowStatus,
    EnumProducerStatus,
    ModelFlowInput,
    NodeDataFlowSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.data-flow-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.data-flow-sweep-completed.v1"


@pytest.mark.unit
class TestDataFlowSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_flowing_pipeline(self, event_bus: EventBusInmemory) -> None:
        """A healthy flow should produce FLOWING status."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.flow.v1",
                    handler_name="projectTest",
                    table_name="test_table",
                    producer_status=EnumProducerStatus.ACTIVE,
                    consumer_lag=0,
                    table_row_count=100,
                    table_has_recent_data=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.status == "healthy"
        assert result.flow_results[0].flow_status == EnumFlowStatus.FLOWING
        assert result.healthy == 1

    async def test_missing_topic(self, event_bus: EventBusInmemory) -> None:
        """A missing topic should produce PRODUCER_DOWN status."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.missing.v1",
                    handler_name="projectMissing",
                    table_name="missing_table",
                    producer_status=EnumProducerStatus.MISSING,
                )
            ]
        )
        result = handler.handle(request)

        assert result.status == "issues_found"
        assert result.flow_results[0].flow_status == EnumFlowStatus.PRODUCER_DOWN

    async def test_empty_table(self, event_bus: EventBusInmemory) -> None:
        """Active topic but empty table should produce EMPTY_TABLE status."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.empty.v1",
                    handler_name="projectEmpty",
                    table_name="empty_table",
                    producer_status=EnumProducerStatus.ACTIVE,
                    table_row_count=0,
                )
            ]
        )
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.EMPTY_TABLE

    async def test_lagging_consumer(self, event_bus: EventBusInmemory) -> None:
        """Consumer with lag > 0 should produce LAGGING status."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.lag.v1",
                    handler_name="projectLag",
                    table_name="lag_table",
                    producer_status=EnumProducerStatus.ACTIVE,
                    consumer_lag=50,
                    table_row_count=100,
                    table_has_recent_data=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.LAGGING

    async def test_stale_data(self, event_bus: EventBusInmemory) -> None:
        """Data older than 24h should produce STALE status."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.stale.v1",
                    handler_name="projectStale",
                    table_name="stale_table",
                    producer_status=EnumProducerStatus.ACTIVE,
                    consumer_lag=0,
                    table_row_count=50,
                    table_has_recent_data=False,
                )
            ]
        )
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.STALE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeDataFlowSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            flows = [ModelFlowInput(**f) for f in payload.get("flows", [])]
            request = DataFlowSweepRequest(flows=flows)
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "flows_checked": result.flows_checked,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-data-flow"
        )

        cmd_payload = json.dumps(
            {
                "flows": [
                    {
                        "topic": "onex.evt.test.ok.v1",
                        "handler_name": "projectOk",
                        "table_name": "ok_table",
                        "producer_status": "ACTIVE",
                        "consumer_lag": 0,
                        "table_row_count": 10,
                        "table_has_recent_data": True,
                    }
                ]
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "healthy"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_dry_run_flag(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(flows=[], dry_run=True)
        result = handler.handle(request)

        assert result.dry_run is True

    async def test_by_status_counts(self, event_bus: EventBusInmemory) -> None:
        """by_status should aggregate flow statuses correctly."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="a",
                    handler_name="a",
                    table_name="a",
                    producer_status=EnumProducerStatus.ACTIVE,
                    table_row_count=10,
                    table_has_recent_data=True,
                ),
                ModelFlowInput(
                    topic="b",
                    handler_name="b",
                    table_name="b",
                    producer_status=EnumProducerStatus.MISSING,
                ),
            ]
        )
        result = handler.handle(request)

        assert result.by_status.get("FLOWING", 0) == 1
        assert result.by_status.get("PRODUCER_DOWN", 0) == 1

    async def test_empty_producer_is_down(self, event_bus: EventBusInmemory) -> None:
        """A topic with EMPTY producer status should be PRODUCER_DOWN."""
        handler = NodeDataFlowSweep()
        request = DataFlowSweepRequest(
            flows=[
                ModelFlowInput(
                    topic="onex.evt.test.empty-producer.v1",
                    handler_name="projectEmptyProd",
                    table_name="empty_prod_table",
                    producer_status=EnumProducerStatus.EMPTY,
                )
            ]
        )
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.PRODUCER_DOWN
