"""Tests for the full workflow runner wiring FSM to orchestrator."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.runtime.runtime_local_adapter import HandlerBusAdapter

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_workflow_runner import (
    HandlerWorkflowRunner,
    ModelWorkflowInput,
    ModelWorkflowOutput,
    run_hostile_review_workflow,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"


class StubAdapter(ModelInferenceAdapter):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        self.calls.append(
            {
                "model_key": model_key,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "timeout_seconds": timeout_seconds,
            }
        )
        return json.dumps(
            [
                {
                    "category": "security",
                    "severity": "minor",
                    "title": "Test",
                    "description": "Test finding from " + model_key,
                }
            ]
        )


@pytest.mark.asyncio
async def test_workflow_runs_through_all_phases():
    result = await run_hostile_review_workflow(
        ModelWorkflowInput(
            correlation_id=uuid4(),
            diff_content="diff --git a/foo.py\n+x = 1",
            model_keys=["test-model"],
            model_context_windows={"test-model": 32_000},
            prompt_template_id="adversarial_reviewer_pr",
        ),
        inference_adapter=StubAdapter(),
    )
    assert isinstance(result, ModelWorkflowOutput)
    assert result.final_phase in {
        EnumHostileReviewerPhase.DONE,
        EnumHostileReviewerPhase.REPORT,
    }
    assert result.orchestrator_output is not None


@pytest.mark.asyncio
async def test_handle_accepts_contract_start_command_payload(tmp_path: Path) -> None:
    review_target = tmp_path / "target.py"
    review_target.write_text("password = request.args['password']\n", encoding="utf-8")
    correlation_id = uuid4()
    adapter = StubAdapter()
    handler = HandlerWorkflowRunner()
    handler.set_adapter(adapter)
    command = ModelHostileReviewerStartCommand(
        correlation_id=correlation_id,
        file_path=str(review_target),
        models=["test-model"],
        dry_run=True,
        requested_at=datetime.now(tz=UTC),
    )

    result = await handler.handle(command.model_dump(mode="json"))

    assert result["correlation_id"] == str(correlation_id)
    assert result["final_phase"] in {"done", "report"}
    assert adapter.calls
    assert "password = request.args" in str(adapter.calls[0]["user_prompt"])


@pytest.mark.asyncio
async def test_handle_strips_runtime_local_transport_metadata(
    tmp_path: Path,
) -> None:
    review_target = tmp_path / "target.py"
    review_target.write_text("token = request.headers['Authorization']\n", encoding="utf-8")
    correlation_id = uuid4()
    adapter = StubAdapter()
    handler = HandlerWorkflowRunner()
    handler.set_adapter(adapter)
    command = ModelHostileReviewerStartCommand(
        correlation_id=correlation_id,
        file_path=str(review_target),
        models=["test-model"],
        dry_run=True,
        requested_at=datetime.now(tz=UTC),
    )
    payload = command.model_dump(mode="json") | {
        "rows": [],
        "event_landed": True,
        "latency_ms": 4.2,
    }

    result = await handler.handle(payload)

    assert result["correlation_id"] == str(correlation_id)
    assert adapter.calls
    assert "Authorization" in str(adapter.calls[0]["user_prompt"])


@pytest.mark.asyncio
async def test_handler_bus_adapter_routes_start_command_envelope(
    event_bus: EventBusInmemory,
    tmp_path: Path,
) -> None:
    review_target = tmp_path / "target.py"
    review_target.write_text("query = f'SELECT {user_input}'\n", encoding="utf-8")
    correlation_id = uuid4()
    adapter = StubAdapter()
    handler = HandlerWorkflowRunner()
    handler.set_adapter(adapter)
    bus_adapter = HandlerBusAdapter(
        handler=handler,
        handler_name="HandlerWorkflowRunner",
        input_model_cls=ModelHostileReviewerStartCommand,
        output_topic=COMPLETED_TOPIC,
        bus=event_bus,
    )
    received: list[dict[str, object]] = []
    done = asyncio.Event()

    async def on_completed(message: object) -> None:
        payload = json.loads(message.value)  # type: ignore[union-attr]
        received.append(payload)
        done.set()

    await event_bus.start()
    try:
        await event_bus.subscribe(
            COMPLETED_TOPIC,
            on_message=on_completed,
            group_id="test-hostile-reviewer-workflow-output",
        )
        await event_bus.subscribe(
            CMD_TOPIC,
            on_message=bus_adapter.on_message,
            group_id="test-hostile-reviewer-workflow-input",
        )
        command = ModelHostileReviewerStartCommand(
            correlation_id=correlation_id,
            file_path=str(review_target),
            models=["test-model"],
            dry_run=True,
            requested_at=datetime.now(tz=UTC),
        )

        await event_bus.publish(
            CMD_TOPIC,
            key=None,
            value=command.model_dump_json().encode("utf-8"),
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)
    finally:
        await event_bus.close()

    assert len(received) == 1
    assert received[0]["correlation_id"] == str(correlation_id)
    assert received[0]["final_phase"] in {"done", "report"}
    assert adapter.calls
    assert "SELECT" in str(adapter.calls[0]["user_prompt"])
