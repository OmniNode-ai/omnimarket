"""Golden chain test for node_golden_chain_sweep.

Verifies the handler can validate chains against projected data,
detect missing fields, and emit completion events via EventBusInmemory.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.golden-chain-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.golden-chain-sweep-completed.v1"


@pytest.mark.unit
class TestGoldenChainSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_all_chains_pass(self, event_bus: EventBusInmemory) -> None:
        """All chains with matching fields should produce overall PASS."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="registration",
                    head_topic="onex.evt.omniclaude.routing-decision.v1",
                    tail_table="agent_routing_decisions",
                    expected_fields=["correlation_id", "selected_agent"],
                )
            ],
            projected_rows={
                "registration": {
                    "correlation_id": "golden-chain-reg-123",
                    "selected_agent": "agent-api-architect",
                }
            },
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_passed == 1
        assert result.chains_failed == 0

    async def test_missing_fields_fail(self, event_bus: EventBusInmemory) -> None:
        """Chains with missing expected fields should FAIL."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="delegation",
                    head_topic="onex.evt.omniclaude.task-delegated.v1",
                    tail_table="delegation_events",
                    expected_fields=["correlation_id", "delegated_to", "task_type"],
                )
            ],
            projected_rows={"delegation": {"correlation_id": "golden-chain-del-456"}},
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.FAIL
        assert result.chain_results[0].status == EnumChainStatus.FAIL
        assert "delegated_to" in result.chain_results[0].missing_fields
        assert "task_type" in result.chain_results[0].missing_fields

    async def test_no_projected_row_timeout(self, event_bus: EventBusInmemory) -> None:
        """Chains with no projected row should produce TIMEOUT."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="routing",
                    head_topic="onex.evt.omniclaude.llm-routing-decision.v1",
                    tail_table="llm_routing_decisions",
                    expected_fields=["correlation_id"],
                )
            ],
            projected_rows={},
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.TIMEOUT

    async def test_partial_status(self, event_bus: EventBusInmemory) -> None:
        """Mix of pass and fail should produce PARTIAL overall status."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="good",
                    head_topic="topic.a",
                    tail_table="table_a",
                    expected_fields=["id"],
                ),
                ModelChainDefinition(
                    name="bad",
                    head_topic="topic.b",
                    tail_table="table_b",
                    expected_fields=["id", "missing_field"],
                ),
            ],
            projected_rows={
                "good": {"id": 1},
                "bad": {"id": 2},
            },
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.PARTIAL
        assert result.chains_passed == 1
        assert result.chains_failed == 1

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeGoldenChainSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            chains = [ModelChainDefinition(**c) for c in payload.get("chains", [])]
            request = GoldenChainSweepRequest(
                chains=chains,
                projected_rows=payload.get("projected_rows", {}),
            )
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "chains_total": result.chains_total,
                "chains_passed": result.chains_passed,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-golden-chain"
        )

        cmd_payload = json.dumps(
            {
                "chains": [
                    {
                        "name": "test",
                        "head_topic": "topic.test",
                        "tail_table": "test_table",
                        "expected_fields": ["id"],
                    }
                ],
                "projected_rows": {"test": {"id": 1}},
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "pass"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_empty_chains(self, event_bus: EventBusInmemory) -> None:
        """Empty chains list should produce PASS with zero counts."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(chains=[])
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_total == 0

    async def test_by_status_counts(self, event_bus: EventBusInmemory) -> None:
        """by_status should aggregate chain statuses correctly."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="a", head_topic="t.a", tail_table="ta", expected_fields=["x"]
                ),
                ModelChainDefinition(
                    name="b", head_topic="t.b", tail_table="tb", expected_fields=["y"]
                ),
            ],
            projected_rows={
                "a": {"x": 1},
            },
        )
        result = handler.handle(request)

        assert result.by_status.get("pass", 0) == 1
        assert result.by_status.get("timeout", 0) == 1

    async def test_no_expected_fields_passes(self, event_bus: EventBusInmemory) -> None:
        """Chain with no expected fields should pass as long as row exists."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[
                ModelChainDefinition(
                    name="simple",
                    head_topic="t.simple",
                    tail_table="simple_table",
                )
            ],
            projected_rows={"simple": {"anything": True}},
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.PASS
