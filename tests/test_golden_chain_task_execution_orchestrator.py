# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_task_execution_orchestrator (OMN-12702).

End-to-end command -> handler -> terminal-event chain over the in-memory bus:

  - a ModelDispatchBusCommand published to the command topic yields a
    ModelDispatchBusTerminalResult on the command's response topic with the
    same correlation_id, a completed status, and the expected route decisions.
  - an unsupported command yields a typed failed terminal result.
  - a raw prompt and the equivalent supplied ModelTaskContract produce the same
    normalized contract fingerprint and route plan (Pattern-B / direct parity).

task.execute COMPOSES existing authorities; it must not become a new authority.
No new envelope and no new DoD model are introduced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.dispatch.model_dispatch_bus_command import (
    ModelDispatchBusCommand,
)
from omnibase_core.models.dispatch.model_dispatch_bus_terminal_result import (
    ModelDispatchBusTerminalResult,
)
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck
from omnibase_core.models.task.model_task_contract import ModelTaskContract

from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    HandlerTaskExecutionOrchestrator,
)
from omnimarket.nodes.node_task_execution_orchestrator.models.model_task_execution import (
    ModelTaskExecutionRequest,
)

TOPIC_TASK_EXECUTE_START = "onex.cmd.omnimarket.task-execute-start.v1"
TOPIC_TASK_EXECUTE_RESPONSE = "onex.evt.omnimarket.task-execute-completed.v1"

_FIXED_GENERATED_AT = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)


def _sample_contract() -> ModelTaskContract:
    return ModelTaskContract(
        task_id="task-1",
        parent_ticket="OMN-12702",
        repo="omnibase_core",
        generated_at=_FIXED_GENERATED_AT,
        requirements=["refactor the config loader"],
        definition_of_done=[
            ModelMechanicalCheck(
                criterion="tests pass",
                check="uv run pytest tests/",
                check_type=EnumCheckType.COMMAND_EXIT_0,
            ),
            ModelMechanicalCheck(
                criterion="no TODO markers remain",
                check="grep -r TODO src/",
                check_type=EnumCheckType.GREP_ABSENT,
            ),
        ],
    )


@pytest.mark.unit
class TestTaskExecutionOrchestratorGoldenChain:
    """Full Pattern-B command -> terminal-result chain over the in-memory bus."""

    async def test_command_to_terminal_result_on_response_topic(self) -> None:
        event_bus = EventBusInmemory(environment="test", group="omnimarket-test")
        await event_bus.start()

        handler = HandlerTaskExecutionOrchestrator(event_bus=event_bus)
        correlation_id = uuid4()
        contract = _sample_contract()

        command = ModelDispatchBusCommand(
            command_name="task.execute",
            requester="golden-chain",
            payload={
                "task_contract": contract.model_dump(mode="json"),
                "dry_run": True,
            },
            correlation_id=correlation_id,
            response_topic=TOPIC_TASK_EXECUTE_RESPONSE,
        )

        # Publish the command to the node's command topic, then drive the
        # consumer with the bytes it would receive from that topic.
        await event_bus.publish(
            topic=TOPIC_TASK_EXECUTE_START,
            key=str(correlation_id).encode("utf-8"),
            value=command.model_dump_json().encode("utf-8"),
        )
        command_history = await event_bus.get_event_history(
            topic=TOPIC_TASK_EXECUTE_START
        )
        assert len(command_history) == 1
        await handler.process(command_history[0].value)

        response_history = await event_bus.get_event_history(
            topic=TOPIC_TASK_EXECUTE_RESPONSE
        )
        assert len(response_history) == 1
        terminal = ModelDispatchBusTerminalResult.model_validate_json(
            response_history[0].value
        )
        assert terminal.correlation_id == correlation_id
        assert terminal.status == "completed"
        assert terminal.error_message is None
        assert isinstance(terminal.payload, dict)
        routes = [d["route"] for d in terminal.payload["route_plan"]]
        assert routes == ["delegation", "verification", "verification"]

        await event_bus.close()

    async def test_unsupported_command_yields_failed_terminal(self) -> None:
        event_bus = EventBusInmemory(environment="test", group="omnimarket-test")
        await event_bus.start()

        handler = HandlerTaskExecutionOrchestrator(event_bus=event_bus)
        correlation_id = uuid4()
        command = ModelDispatchBusCommand(
            command_name="task.execute",
            requester="golden-chain",
            payload={"dry_run": True},  # neither prompt nor task_contract
            correlation_id=correlation_id,
            response_topic=TOPIC_TASK_EXECUTE_RESPONSE,
        )

        await handler.process(command.model_dump_json().encode("utf-8"))

        response_history = await event_bus.get_event_history(
            topic=TOPIC_TASK_EXECUTE_RESPONSE
        )
        assert len(response_history) == 1
        terminal = ModelDispatchBusTerminalResult.model_validate_json(
            response_history[0].value
        )
        assert terminal.correlation_id == correlation_id
        assert terminal.status == "failed"
        assert terminal.error_message is not None

        await event_bus.close()

    def test_prompt_and_contract_paths_produce_equivalent_plan(self) -> None:
        """Direct prompt path and supplied-contract path do not drift."""
        handler = HandlerTaskExecutionOrchestrator()
        prompt = "refactor the config loader"

        from_prompt = handler.handle(
            ModelTaskExecutionRequest(prompt=prompt, target_repo="omnibase_core")
        )
        equivalent_contract = ModelTaskContract(
            task_id=from_prompt.task_contract.task_id,
            repo="omnibase_core",
            generated_at=_FIXED_GENERATED_AT,
            generated_by="node_task_execution_orchestrator",
            requirements=[prompt],
        )
        from_contract = handler.handle(
            ModelTaskExecutionRequest(task_contract=equivalent_contract)
        )

        assert from_prompt.contract_fingerprint == from_contract.contract_fingerprint
        assert from_prompt.route_plan == from_contract.route_plan
