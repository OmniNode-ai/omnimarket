# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19458 -- the focused run over the bus, on the in-memory bus.

The loop side publishes a request on the node's command topic; the lab-host
side runs it and answers on the terminal; the loop side resolves the request
from the terminal whose parent envelope id is its own command's.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    FocusedRunBusCaller,
    FocusedRunHost,
    load_focused_run_topics,
)
from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunReceipt,
    ModelFocusedTestRunRequest,
)

pytestmark = pytest.mark.unit

_SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
_CID = "f9c814aa-d101-416f-8b5d-605d6e6609f1"


def _request(**overrides: object) -> ModelFocusedTestRunRequest:
    fields: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": _SHA,
        "test_node_id": "tests/unit/test_x.py",
        "timeout_seconds": 60,
        "correlation_id": _CID,
        "ref_role": "fixed",
        "attempt": 1,
    }
    fields.update(overrides)
    return ModelFocusedTestRunRequest.model_validate(fields)


class _FakeHandler:
    """Answers each request with a completed receipt naming its role."""

    def __init__(self, *, raises: bool = False) -> None:
        self.requests: list[ModelFocusedTestRunRequest] = []
        self._raises = raises

    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        self.requests.append(request)
        if self._raises:
            raise RuntimeError("docker daemon is gone")
        return ModelFocusedTestRunReceipt(
            correlation_id=request.correlation_id,
            ref_role=request.ref_role,
            attempt=request.attempt,
            commit_sha=request.commit_sha,
            test_node_id=request.test_node_id,
            status=EnumFocusedTestRunStatus.COMPLETED,
            exit_code=0,
            junit_xml=f"<testsuites name='{request.ref_role}-{request.attempt}'/>",
            host="local:lab",
            teardown_container_absent=True,
            teardown_worktree_absent=True,
        )


async def _bus() -> EventBusInmemory:
    bus = EventBusInmemory(environment="local", group="test")
    await bus.start()
    return bus


def test_the_topics_are_the_contract_runtime_dispatch() -> None:
    text = (
        resources.files("omnimarket.nodes.node_focused_test_run_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = yaml.safe_load(text)["runtime_dispatch"]
    topics = load_focused_run_topics()
    assert topics.command == dispatch["command_topic"]
    assert topics.success == dispatch["terminal_events"]["success"]
    assert topics.failure == dispatch["terminal_events"]["failure"]


async def test_a_request_is_run_by_the_host_and_its_receipt_comes_back() -> None:
    bus = await _bus()
    handler = _FakeHandler()
    host = FocusedRunHost(bus, handler)
    client = FocusedRunBusCaller(bus)
    await host.start()
    await client.start()

    receipt = await client.run(_request())

    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert receipt.host == "local:lab"
    assert [r.ref_role for r in handler.requests] == ["fixed"]
    await host.stop()


async def test_runs_sharing_a_correlation_id_each_get_their_own_receipt() -> None:
    bus = await _bus()
    host = FocusedRunHost(bus, _FakeHandler())
    client = FocusedRunBusCaller(bus)
    await host.start()
    await client.start()

    fixed = await client.run(_request(ref_role="fixed", attempt=2))
    prefix = await client.run(_request(ref_role="prefix", attempt=2))

    assert "fixed-2" in fixed.junit_xml
    assert "prefix-2" in prefix.junit_xml
    await host.stop()


async def test_a_terminal_answering_another_command_is_not_taken() -> None:
    bus = await _bus()
    topics = load_focused_run_topics()
    client = FocusedRunBusCaller(bus, wait_slack_seconds=0)
    await client.start()
    # A retained terminal for the same correlation id but a different command.
    stray = ModelEventEnvelope[dict[str, object]](
        payload={"status": "completed"},
        correlation_id=uuid.UUID(_CID),
        parent_envelope_id=uuid.uuid4(),
    )
    await bus.publish(
        topics.success, None, json.dumps(stray.model_dump(mode="json")).encode()
    )

    receipt = await client.run(_request(timeout_seconds=1))

    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "no terminal" in receipt.detail


async def test_a_handler_crash_comes_back_as_an_infra_error_never_a_pass() -> None:
    bus = await _bus()
    host = FocusedRunHost(bus, _FakeHandler(raises=True))
    client = FocusedRunBusCaller(bus)
    await host.start()
    await client.start()

    receipt = await client.run(_request())

    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "docker daemon is gone" in receipt.detail
    await host.stop()


async def test_a_stale_command_is_answered_and_not_run() -> None:
    bus = await _bus()
    handler = _FakeHandler()
    later = datetime.now(UTC) + timedelta(hours=2)
    host = FocusedRunHost(bus, handler, max_command_age_seconds=900, now=lambda: later)
    client = FocusedRunBusCaller(bus)
    await host.start()
    await client.start()

    receipt = await client.run(_request())

    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "was not run" in receipt.detail
    assert handler.requests == []
    await host.stop()


async def test_an_unreadable_command_goes_to_the_failure_terminal() -> None:
    bus = await _bus()
    topics = load_focused_run_topics()
    host = FocusedRunHost(bus, _FakeHandler())
    await host.start()
    failures: list[dict[str, object]] = []

    async def capture(message: object) -> None:
        failures.append(json.loads(message.value))  # type: ignore[attr-defined]

    await bus.subscribe(topics.failure, on_message=capture, group_id="capture")
    command = ModelEventEnvelope[dict[str, object]](
        payload={"repo": "not a slug"}, correlation_id=uuid.UUID(_CID)
    )
    await bus.publish(
        topics.command, None, json.dumps(command.model_dump(mode="json")).encode()
    )
    await host.drain()

    assert len(failures) == 1
    assert failures[0]["parent_envelope_id"] == str(command.envelope_id)
    payload = failures[0]["payload"]
    assert isinstance(payload, dict)
    assert "not a focused-run request" in str(payload["error_message"])
    await host.stop()


async def test_with_no_host_serving_the_client_times_out_as_an_infra_error() -> None:
    bus = await _bus()
    client = FocusedRunBusCaller(bus, wait_slack_seconds=0)
    await client.start()

    receipt = await client.run(_request(timeout_seconds=1))

    assert receipt.status is EnumFocusedTestRunStatus.INFRA_ERROR
    assert "is a lab host serving" in receipt.detail


async def test_a_terminal_for_another_command_arriving_mid_run_is_not_taken() -> None:
    bus = await _bus()
    topics = load_focused_run_topics()
    client = FocusedRunBusCaller(bus)
    await client.start()

    def _terminal(parent: uuid.UUID | None, role: str) -> bytes:
        receipt = ModelFocusedTestRunReceipt(
            correlation_id=_CID,
            ref_role=role,
            attempt=1,
            commit_sha=_SHA,
            test_node_id="tests/unit/test_x.py",
            status=EnumFocusedTestRunStatus.COMPLETED,
            exit_code=0,
            junit_xml=f"<testsuites name='{role}'/>",
            teardown_container_absent=True,
            teardown_worktree_absent=True,
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            correlation_id=uuid.UUID(_CID),
            parent_envelope_id=parent,
        )
        return json.dumps(envelope.model_dump(mode="json")).encode()

    async def answer_twice(message: object) -> None:
        command = json.loads(message.value)  # type: ignore[attr-defined]
        # Another loop's run under the same correlation id answers first.
        await bus.publish(topics.success, None, _terminal(uuid.uuid4(), "stray"))
        await bus.publish(
            topics.success,
            None,
            _terminal(uuid.UUID(command["envelope_id"]), "fixed"),
        )

    await bus.subscribe(topics.command, on_message=answer_twice, group_id="fake-host")

    receipt = await client.run(_request())

    assert "fixed" in receipt.junit_xml
