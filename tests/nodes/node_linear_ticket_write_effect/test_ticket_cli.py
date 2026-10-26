# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI commands publish typed payloads and return bus coordinates, without MCP."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.ticket import ticket_cli
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteRequest,
)

pytestmark = pytest.mark.unit


@dataclass
class Message:
    value: bytes
    partition: int = 2
    offset: str = "42"


class Bus:
    def __init__(self, *, fail=False, answer=True):
        self.callbacks = {}
        self.commands = []
        self.fail = fail
        self.answer = answer

    async def subscribe(
        self, topic, node_identity=None, on_message=None, *, group_id=None
    ):
        assert on_message is not None
        self.callbacks[topic] = on_message

        async def unsubscribe():
            del self.callbacks[topic]

        return unsubscribe

    async def publish(self, topic, key, value, headers=None):
        topics = ticket_cli.load_ticket_topics()
        assert topic == topics.command
        assert topics.success in self.callbacks
        assert topics.failure in self.callbacks
        envelope = ModelEventEnvelope[dict].model_validate_json(value)
        command = ModelTicketWriteRequest.model_validate(envelope.payload)
        self.commands.append(command)
        if self.answer:
            terminal = ModelEventEnvelope(
                payload={
                    "outcome": "failed" if self.fail else "completed",
                    "operation": command.operation.value,
                },
                parent_envelope_id=envelope.envelope_id,
                correlation_id=command.correlation_id,
            )
            await self.callbacks[topics.failure if self.fail else topics.success](
                Message(terminal.model_dump_json().encode())
            )


def install(monkeypatch, bus):
    @contextlib.asynccontextmanager
    async def opened(_):
        yield bus

    monkeypatch.setattr(ticket_cli, "_open_bus", opened)


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        (
            "create",
            ["--title", "Scratch", "--description", "body", "--parent", "OMN-20595"],
        ),
        ("read", ["OMN-20570"]),
        ("comment", ["OMN-20570", "--body", "proof"]),
        ("transition", ["OMN-20570", "--state", "Canceled"]),
    ],
)
def test_each_operation_publishes_lane_run_and_terminal_coordinates(
    monkeypatch, operation, args
):
    bus = Bus()
    install(monkeypatch, bus)
    result = CliRunner().invoke(
        ticket_cli.ticket_group,
        [operation, *args, "--lane", "lane-a", "--run", "run-a"],
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["receipt"]["terminal_partition"] == 2
    assert answer["receipt"]["terminal_offset"] == "42"
    assert answer["receipt"]["terminal_envelope_id"]
    assert bus.commands[0].requested_by_lane == "lane-a"
    assert bus.commands[0].requested_by_run == "run-a"
    assert bus.commands[0].operation == operation
    assert bus.callbacks == {}


def test_description_file_preserves_crlf_and_stable_replay_key(
    monkeypatch, tmp_path: Path
):
    body = b"created-by-lane: lane-a\r\n\r\nGate: OmniNode-ai/omnimarket ci.yml\r\n"
    path = tmp_path / "body.txt"
    path.write_bytes(body)
    bus = Bus()
    install(monkeypatch, bus)
    for _ in range(2):
        result = CliRunner().invoke(
            ticket_cli.ticket_group,
            [
                "create",
                "--title",
                "Scratch",
                "--description-file",
                str(path),
                "--lane",
                "lane-a",
            ],
        )
        assert result.exit_code == 0
    a, b = bus.commands
    assert a.description.encode() == body
    assert a.idempotency_key == b.idempotency_key
    assert a.correlation_id != b.correlation_id


def test_failed_terminal_and_timeout_are_distinct(monkeypatch):
    bus = Bus(fail=True)
    install(monkeypatch, bus)
    failed = CliRunner().invoke(
        ticket_cli.ticket_group, ["read", "OMN-20570", "--lane", "lane-a"]
    )
    assert failed.exit_code == 1
    assert json.loads(failed.output)["failed"] is True
    bus = Bus(answer=False)
    install(monkeypatch, bus)
    timeout = CliRunner().invoke(
        ticket_cli.ticket_group,
        ["read", "OMN-20570", "--lane", "lane-a", "--timeout-s", "0.1"],
    )
    assert timeout.exit_code == 75
    assert json.loads(timeout.output)["status"] == "no_terminal"
    assert len(bus.commands) == 1
    assert bus.callbacks == {}
