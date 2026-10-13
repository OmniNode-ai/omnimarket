# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""design-to-plan must carry a real plan on its completed event (OMN-21038).

Before this change ``HandlerDesignToPlan.handle()`` advanced an FSM whose phases
did no work: a start command with only a ``topic`` completed in milliseconds with
``plan_path=None`` and no plan anywhere on the event. These tests pin the repaired
contract:

* a start command without ``plan_path`` is brainstormed into a markdown plan by a
  model reached through the shared inference bridge, and that plan rides on the
  completed event as ``plan_content``;
* the plan parses into the structured plan model (``HandlerPlanToTickets``);
* a model answer that is not a plan, or a transport failure, stops the FSM short
  of DONE and the event carries no plan;
* with the launch off nothing but the completed event is published, in particular
  no ``plan-to-tickets-start`` command.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_result import (
    ModelDesignToPlanResult,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    EnumDesignToPlanPhase,
)
from omnimarket.nodes.node_plan_to_tickets.handlers.handler_plan_to_tickets import (
    HandlerPlanToTickets,
    ModelPlanToTicketsRequest,
)

_PLAN = """# Quiet hours for reminders

## Task 1: Add the quiet-hours setting
Store a daily start and end time per user and expose it in the settings screen.

## Task 2: Hold reminders inside the window
Dependencies: Task 1
Suppress reminder notifications raised inside the window and queue them.

## Task 3: Deliver one summary at window end
Dependencies: Task 2
When the window ends, send the queued reminders as a single summary notification.
"""

COMPLETED_TOPIC = "onex.evt.omnimarket.design-to-plan-completed.v1"
PLAN_TO_TICKETS_TOPIC = "onex.cmd.omnimarket.plan-to-tickets-start.v1"


class _ModelServer:
    """A real loopback OpenAI-style chat-completions server.

    The handler under test talks to it through the real ``AdapterInferenceBridge``
    (real request construction, real HTTP), so no inference boundary is faked.
    """

    def __init__(self) -> None:
        self.status = 200
        self.content = ""
        self.requests: list[dict[str, object]] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps(
                    {"choices": [{"message": {"content": outer.content}}]}
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def bridge(self) -> AdapterInferenceBridge:
        return AdapterInferenceBridge(
            ModelInferenceBridgeConfig(
                model_configs={
                    "qwen3-coder": {
                        "base_url": self.url,
                        "model_id": "lab-test-model",
                        "transport": "http",
                    }
                }
            )
        )


@pytest.fixture
def model_server() -> Iterator[_ModelServer]:
    server = _ModelServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


class _CapturingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, key: object, value: bytes) -> None:
        self.published.append((topic, value))


class _Msg:
    def __init__(self, value: bytes) -> None:
        self.value = value


def _command(
    *, plan_path: str | None = None, no_launch: bool = True
) -> ModelDesignToPlanCommand:
    return ModelDesignToPlanCommand(
        correlation_id=uuid4(),
        topic="Add quiet hours that batch reminder notifications",
        plan_path=plan_path,
        no_launch=no_launch,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
async def test_completed_event_carries_plan_that_parses(
    model_server: _ModelServer,
) -> None:
    model_server.content = _PLAN
    result = await HandlerDesignToPlan(inference_bridge=model_server.bridge()).handle(
        _command()
    )

    completed = result.completed_event
    assert completed.final_phase == EnumDesignToPlanPhase.DONE
    assert completed.error_message is None
    assert completed.plan_content is not None
    assert len(model_server.requests) == 1
    assert model_server.requests[0]["model"] == "lab-test-model"
    assert "quiet hours" in json.dumps(model_server.requests[0]["messages"])

    parsed = HandlerPlanToTickets().handle(
        ModelPlanToTicketsRequest(plan_content=completed.plan_content, dry_run=True)
    )
    assert parsed.status == "parsed"
    assert parsed.entry_count == 3
    assert parsed.entries[1].dependencies == ["P1"]


@pytest.mark.unit
async def test_unstructured_model_answer_stops_short_of_done_with_no_plan(
    model_server: _ModelServer,
) -> None:
    model_server.content = "Sure! Here are some thoughts about quiet hours."
    result = await HandlerDesignToPlan(inference_bridge=model_server.bridge()).handle(
        _command()
    )

    assert result.completed_event.final_phase != EnumDesignToPlanPhase.DONE
    assert result.completed_event.plan_content is None
    assert result.completed_event.error_message is not None
    assert "structure" in result.completed_event.error_message.lower()


@pytest.mark.unit
async def test_model_server_error_stops_short_of_done_with_no_plan(
    model_server: _ModelServer,
) -> None:
    model_server.status = 500
    result = await HandlerDesignToPlan(inference_bridge=model_server.bridge()).handle(
        _command()
    )

    assert result.completed_event.final_phase == EnumDesignToPlanPhase.IDLE
    assert result.completed_event.plan_content is None
    assert result.completed_event.error_message is not None
    assert "brainstorm" in result.completed_event.error_message


@pytest.mark.unit
async def test_existing_plan_path_skips_the_model(model_server: _ModelServer) -> None:
    result = await HandlerDesignToPlan(inference_bridge=model_server.bridge()).handle(
        _command(plan_path="/tmp/existing-plan.md")
    )

    assert model_server.requests == []
    assert result.completed_event.final_phase == EnumDesignToPlanPhase.DONE
    assert result.completed_event.plan_path == "/tmp/existing-plan.md"


@pytest.mark.unit
async def test_launch_off_publishes_only_the_completed_event(
    model_server: _ModelServer,
) -> None:
    model_server.content = _PLAN
    errors: list[str] = []
    bus = _CapturingBus()
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerDesignToPlan(inference_bridge=model_server.bridge()),
        handler_name="HandlerDesignToPlan",
        input_model_cls=ModelDesignToPlanCommand,
        output_topic=COMPLETED_TOPIC,
        bus=bus,
        on_error=lambda: errors.append("FAILED"),
    )
    command = _command(no_launch=True)

    await adapter.on_message(
        _Msg(json.dumps(command.model_dump(mode="json")).encode("utf-8"))
    )

    assert errors == []
    assert [topic for topic, _ in bus.published] == [COMPLETED_TOPIC]
    assert PLAN_TO_TICKETS_TOPIC not in [topic for topic, _ in bus.published]
    result = ModelDesignToPlanResult.model_validate_json(bus.published[0][1])
    assert result.completed_event.correlation_id == command.correlation_id
    assert result.completed_event.plan_content == _PLAN.strip()
