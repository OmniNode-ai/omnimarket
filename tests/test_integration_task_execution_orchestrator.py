# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-12706 reason="integration test — uses lab Kafka bootstrap address as opt-in test target; not a runtime default"
"""OMN-12706: Pattern-B runtime integration + prompt/contract parity tests.

Phase 6 of the generic ``task.execute`` route. These tests drive the
node_task_execution_orchestrator handler (OMN-12702..12705) through a real
event bus, end to end:

  command published to the command topic
    -> bus-delivered to the subscribed consumer (handler.process)
      -> handler plans / composes existing authorities
        -> terminal result published to the command's response topic.

Governing principle (unchanged from the slice tickets): task.execute COMPOSES
existing authorities; it must NOT become a new authority. No new envelope, no new
DoD model. Runtime dispatch is topic-based — the command is delivered over the
bus to the consumer, never via a generic command_name router.

DoD coverage:

  - in-memory bus: raw prompt -> task contract / route plan terminal result.
    (``test_inmemory_prompt_yields_contract_and_route_plan``)
  - in-memory bus: mechanical pytest check -> verification evidence terminal
    result, executed by the verification authority (real subprocess, no mock).
    (``test_inmemory_mechanical_pytest_check_yields_verification_evidence``)
  - in-memory bus: dry-run PR action -> PR plan terminal result, no side effects.
    (``test_inmemory_dry_run_pr_action_yields_pr_plan``)
  - parity: raw prompt AND the equivalent supplied ModelTaskContract produce the
    same normalized contract and the same route plan, both over the bus.
    (``test_inmemory_prompt_and_contract_parity_over_bus``)
  - Redpanda bus: the same core flows behind the opt-in ``RUN_REDPANDA_TASK_EXECUTE``
    env flag, skipped when the broker is unavailable.
    (``TestRedpandaTaskExecutionIntegration``)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck
from omnibase_core.models.task.model_task_contract import ModelTaskContract

from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    HandlerTaskExecutionOrchestrator,
)

TOPIC_TASK_EXECUTE_START = "onex.cmd.omnimarket.task-execute-start.v1"
TOPIC_TASK_EXECUTE_RESPONSE = "onex.evt.omnimarket.task-execute-completed.v1"

_FIXED_GENERATED_AT = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)

_REDPANDA_FLAG = "RUN_REDPANDA_TASK_EXECUTE"
_REDPANDA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")


# ---------------------------------------------------------------------------
# Shared fixture material
# ---------------------------------------------------------------------------
def _prompt() -> str:
    return "refactor the config loader"


def _branch_contract() -> ModelTaskContract:
    """A contract that declares a branch (required for the create_pr path)."""
    return ModelTaskContract(
        task_id="task-pr",
        parent_ticket="OMN-12706",
        repo="omnimarket",
        branch="jonah/omn-12706-integration-tests",
        generated_at=_FIXED_GENERATED_AT,
        requirements=["add the integration tests"],
        definition_of_done=[
            ModelMechanicalCheck(
                criterion="tests pass",
                check="uv run pytest tests/",
                check_type=EnumCheckType.COMMAND_EXIT_0,
            ),
        ],
    )


def _command(payload: dict[str, object], *, correlation_id: uuid.UUID) -> bytes:
    command = ModelDispatchBusCommand(
        command_name="task.execute",
        requester="omn-12706-integration",
        payload=payload,
        correlation_id=correlation_id,
        response_topic=TOPIC_TASK_EXECUTE_RESPONSE,
        created_at=_FIXED_GENERATED_AT,
    )
    return command.model_dump_json().encode("utf-8")


def _completed_payload(
    terminal: ModelDispatchBusTerminalResult,
) -> dict[str, Any]:
    """Assert the terminal completed and return its payload as a typed dict.

    Narrows ``ModelDispatchBusTerminalResult.payload`` (a JsonType union) to a
    concrete ``dict[str, Any]`` so the per-DoD assertions index it unambiguously.
    """
    assert terminal.status == "completed", terminal.error_message
    assert terminal.error_message is None
    assert isinstance(terminal.payload, dict)
    return terminal.payload


def _as_dict(value: Any) -> dict[str, Any]:
    """Narrow a nested JSON value to a dict for unambiguous indexing."""
    assert isinstance(value, dict)
    return value


async def _run_inmemory_flow(
    payload: dict[str, object],
) -> ModelDispatchBusTerminalResult:
    """Drive one command fully through the in-memory bus and return the terminal.

    The handler's ``process`` is wired as a real topic subscriber, so publishing
    the command to the command topic causes the bus to deliver it to the
    consumer, which plans/composes and publishes the terminal result to the
    response topic. The flow is the actual runtime topic-based dispatch path
    (Pattern-B), not a direct in-process call.
    """
    event_bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await event_bus.start()
    handler = HandlerTaskExecutionOrchestrator(event_bus=event_bus)

    async def on_command(message: ModelEventMessage) -> None:
        # The consumer receives exactly the bytes published to the command topic
        # and emits the terminal result to the command's response_topic.
        await handler.process(message.value)

    unsubscribe = await event_bus.subscribe(
        TOPIC_TASK_EXECUTE_START,
        on_message=on_command,
        group_id="omn-12706-task-execute-consumer",
    )
    try:
        correlation_id = uuid4()
        await event_bus.publish(
            topic=TOPIC_TASK_EXECUTE_START,
            key=str(correlation_id).encode("utf-8"),
            value=_command(payload, correlation_id=correlation_id),
        )
        history = await event_bus.get_event_history(topic=TOPIC_TASK_EXECUTE_RESPONSE)
        assert len(history) == 1, "exactly one terminal result on the response topic"
        terminal = ModelDispatchBusTerminalResult.model_validate_json(history[0].value)
        assert terminal.correlation_id == correlation_id
        return terminal
    finally:
        await unsubscribe()
        await event_bus.close()


def _write_passing_pytest_worktree(work: Path) -> str:
    """Write a trivial, deterministic passing pytest into ``work``.

    Used as a real mechanical ``COMMAND_EXIT_0`` check so the verification
    authority executes an actual pytest invocation (no mock) and the terminal
    result carries genuine verification evidence.
    """
    (work / "test_trivial.py").write_text(
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    # Run pytest in-process via the current interpreter; quiet + no plugins for a
    # deterministic, side-effect-free exit code.
    return "python -m pytest test_trivial.py -p no:cacheprovider -q"


# ---------------------------------------------------------------------------
# In-memory bus integration
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
class TestInMemoryTaskExecutionIntegration:
    """Pattern-B runtime integration over the in-memory bus (no broker)."""

    async def test_inmemory_prompt_yields_contract_and_route_plan(self) -> None:
        """DoD: raw prompt -> task contract / route plan terminal result."""
        terminal = await _run_inmemory_flow(
            {
                "prompt": _prompt(),
                "target_repo": "omnibase_core",
                "dry_run": True,
            }
        )

        payload = _completed_payload(terminal)
        # A normalized contract was produced from the raw prompt.
        contract = _as_dict(payload["task_contract"])
        assert contract["requirements"] == [_prompt()]
        assert contract["repo"] == "omnibase_core"
        assert payload["contract_fingerprint"]
        # The route plan maps the single requirement to the delegation route.
        routes = [_as_dict(decision)["route"] for decision in payload["route_plan"]]
        assert routes == ["delegation"]

    async def test_inmemory_mechanical_pytest_check_yields_verification_evidence(
        self, tmp_path: Path
    ) -> None:
        """DoD: mechanical pytest check -> verification evidence terminal result.

        A real passing pytest is executed by node_verification_receipt_generator
        (the verification authority) as a COMMAND_EXIT_0 mechanical check.
        task.execute composes that authority and aggregates its receipt UNCHANGED;
        the terminal result carries the receipt's own evidence.
        """
        check_command = _write_passing_pytest_worktree(tmp_path)
        contract = ModelTaskContract(
            task_id="task-mechanical-pytest",
            parent_ticket="OMN-12706",
            repo="omnimarket",
            generated_at=_FIXED_GENERATED_AT,
            requirements=["keep the suite green"],
            definition_of_done=[
                ModelMechanicalCheck(
                    criterion="pytest passes",
                    check=check_command,
                    check_type=EnumCheckType.COMMAND_EXIT_0,
                ),
            ],
        )

        terminal = await _run_inmemory_flow(
            {
                "task_contract": contract.model_dump(mode="json"),
                "execute_mechanical_checks": True,
                "worktree_path": str(tmp_path),
                "dry_run": True,
            }
        )

        payload = _completed_payload(terminal)
        # Terminal ok is derived from the receipt's overall_pass, never re-decided.
        assert payload["ok"] is True
        assert payload["failure_reason"] is None
        receipt = _as_dict(payload["verification_receipt"])
        assert receipt["overall_pass"] is True
        # The verification authority's own evidence is present, aggregated verbatim.
        checks = [_as_dict(check) for check in receipt["checks"]]
        dimensions = [check["dimension"] for check in checks]
        assert any("pytest passes" in str(dimension) for dimension in dimensions)
        assert all(check["passed"] for check in checks)
        # The mechanical check planned to the verification route.
        routes = [_as_dict(decision)["route"] for decision in payload["route_plan"]]
        assert "verification" in routes

    async def test_inmemory_dry_run_pr_action_yields_pr_plan(self) -> None:
        """DoD: dry-run PR action -> PR plan terminal result, no side effects."""
        terminal = await _run_inmemory_flow(
            {
                "task_contract": _branch_contract().model_dump(mode="json"),
                "create_pr": True,
                "dry_run": True,
                "worktree_path": "/work/tree",
            }
        )

        payload = _completed_payload(terminal)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        # Dry-run performs NO side effects: a plan is returned, no result.
        assert payload["pr_result"] is None
        pr_plan = _as_dict(payload["pr_plan"])
        assert pr_plan["branch"] == "jonah/omn-12706-integration-tests"
        assert pr_plan["worktree_path"] == "/work/tree"
        assert str(pr_plan["title"]).startswith("OMN-12706:")
        assert "add the integration tests" in str(pr_plan["body"])
        assert "## Definition of Done" in str(pr_plan["body"])

    async def test_inmemory_prompt_and_contract_parity_over_bus(self) -> None:
        """DoD: prompt path and equivalent-contract path do not drift over the bus.

        The raw prompt is normalized to a contract; an equivalent supplied
        ModelTaskContract (same task_id, repo, generated_at, generated_by, and
        requirement) must yield the SAME normalized contract and the SAME route
        plan when both are driven through the bus.
        """
        prompt_payload = _completed_payload(
            await _run_inmemory_flow(
                {"prompt": _prompt(), "target_repo": "omnibase_core", "dry_run": True}
            )
        )
        normalized = _as_dict(prompt_payload["task_contract"])

        equivalent_contract = ModelTaskContract(
            task_id=str(normalized["task_id"]),
            repo="omnibase_core",
            generated_at=_FIXED_GENERATED_AT,
            generated_by="node_task_execution_orchestrator",
            requirements=[_prompt()],
        )
        contract_payload = _completed_payload(
            await _run_inmemory_flow(
                {
                    "task_contract": equivalent_contract.model_dump(mode="json"),
                    "dry_run": True,
                }
            )
        )

        # Same normalized contract.
        assert prompt_payload["task_contract"] == contract_payload["task_contract"]
        # Same fingerprint and same route plan — the two entry paths are coherent.
        assert (
            prompt_payload["contract_fingerprint"]
            == contract_payload["contract_fingerprint"]
        )
        assert prompt_payload["route_plan"] == contract_payload["route_plan"]


# ---------------------------------------------------------------------------
# Redpanda bus integration (opt-in, skipped when broker unavailable)
# ---------------------------------------------------------------------------
async def _probe_redpanda() -> None:
    """Skip unless the opt-in flag is set AND a broker is actually reachable."""
    if os.environ.get(_REDPANDA_FLAG) != "1":
        pytest.skip(
            f"set {_REDPANDA_FLAG}=1 (and KAFKA_BOOTSTRAP_SERVERS) to run the "
            "Redpanda task.execute integration flows"
        )
    from aiokafka import AIOKafkaProducer

    try:
        probe = AIOKafkaProducer(bootstrap_servers=_REDPANDA_BOOTSTRAP)
        await asyncio.wait_for(probe.start(), timeout=5)
        await probe.stop()
    except Exception as exc:
        pytest.skip(f"Redpanda broker not reachable at {_REDPANDA_BOOTSTRAP}: {exc}")


async def _run_redpanda_flow(
    payload: dict[str, object],
) -> ModelDispatchBusTerminalResult:
    """Drive one command through a real Redpanda broker and return the terminal.

    Mirrors the in-memory flow over aiokafka: the command is produced to the
    command topic, consumed by a consumer that runs the handler's ``process``,
    and the terminal result is consumed back off the response topic. The same
    handler code path runs — only the transport differs.
    """
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    suffix = uuid.uuid4().hex[:8]
    command_topic = f"{TOPIC_TASK_EXECUTE_START}.it-{suffix}"
    response_topic = f"{TOPIC_TASK_EXECUTE_RESPONSE}.it-{suffix}"
    correlation_id = uuid4()

    command = ModelDispatchBusCommand(
        command_name="task.execute",
        requester="omn-12706-redpanda-integration",
        payload=payload,
        correlation_id=correlation_id,
        response_topic=response_topic,
        created_at=_FIXED_GENERATED_AT,
    )

    class _AioKafkaPublisher:
        """Minimal ProtocolTaskExecutionPublisher backed by an aiokafka producer."""

        def __init__(self, producer: AIOKafkaProducer) -> None:
            self._producer = producer

        async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
            await self._producer.send_and_wait(topic, value=value, key=key)

    producer = AIOKafkaProducer(bootstrap_servers=_REDPANDA_BOOTSTRAP)
    await producer.start()
    response_consumer = AIOKafkaConsumer(
        response_topic,
        bootstrap_servers=_REDPANDA_BOOTSTRAP,
        group_id=f"omn-12706-response-{suffix}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    command_consumer = AIOKafkaConsumer(
        command_topic,
        bootstrap_servers=_REDPANDA_BOOTSTRAP,
        group_id=f"omn-12706-command-{suffix}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await response_consumer.start()
    await command_consumer.start()
    try:
        handler = HandlerTaskExecutionOrchestrator(
            event_bus=_AioKafkaPublisher(producer)
        )
        await producer.send_and_wait(
            command_topic,
            value=command.model_dump_json().encode("utf-8"),
            key=str(correlation_id).encode("utf-8"),
        )
        # Consume the command, run the real handler, publish the terminal result.
        command_msg = await asyncio.wait_for(command_consumer.getone(), timeout=15)
        await handler.process(command_msg.value)
        # Consume the terminal result back off the response topic.
        response_msg = await asyncio.wait_for(response_consumer.getone(), timeout=15)
        return ModelDispatchBusTerminalResult.model_validate_json(response_msg.value)
    finally:
        await command_consumer.stop()
        await response_consumer.stop()
        await producer.stop()


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
class TestRedpandaTaskExecutionIntegration:
    """The same core flows over a real Redpanda broker (opt-in, skip if absent).

    Behind the ``RUN_REDPANDA_TASK_EXECUTE=1`` flag and a reachable broker; both
    gates skip otherwise so the default suite never requires infrastructure.
    """

    async def test_redpanda_prompt_yields_contract_and_route_plan(self) -> None:
        await _probe_redpanda()
        payload = _completed_payload(
            await _run_redpanda_flow(
                {"prompt": _prompt(), "target_repo": "omnibase_core", "dry_run": True}
            )
        )
        assert _as_dict(payload["task_contract"])["requirements"] == [_prompt()]
        routes = [_as_dict(decision)["route"] for decision in payload["route_plan"]]
        assert routes == ["delegation"]

    async def test_redpanda_dry_run_pr_action_yields_pr_plan(self) -> None:
        await _probe_redpanda()
        terminal = await _run_redpanda_flow(
            {
                "task_contract": _branch_contract().model_dump(mode="json"),
                "create_pr": True,
                "dry_run": True,
                "worktree_path": "/work/tree",
            }
        )
        payload = _completed_payload(terminal)
        assert payload["pr_result"] is None
        assert (
            _as_dict(payload["pr_plan"])["branch"]
            == "jonah/omn-12706-integration-tests"
        )

    async def test_redpanda_prompt_and_contract_parity(self) -> None:
        await _probe_redpanda()
        prompt_payload = _completed_payload(
            await _run_redpanda_flow(
                {"prompt": _prompt(), "target_repo": "omnibase_core", "dry_run": True}
            )
        )
        normalized = _as_dict(prompt_payload["task_contract"])
        equivalent_contract = ModelTaskContract(
            task_id=str(normalized["task_id"]),
            repo="omnibase_core",
            generated_at=_FIXED_GENERATED_AT,
            generated_by="node_task_execution_orchestrator",
            requirements=[_prompt()],
        )
        contract_payload = _completed_payload(
            await _run_redpanda_flow(
                {
                    "task_contract": equivalent_contract.model_dump(mode="json"),
                    "dry_run": True,
                }
            )
        )
        assert prompt_payload["task_contract"] == contract_payload["task_contract"]
        assert (
            prompt_payload["contract_fingerprint"]
            == contract_payload["contract_fingerprint"]
        )
        assert prompt_payload["route_plan"] == contract_payload["route_plan"]
