# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction pack commands and both terminals cross the real in-memory bus."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.models.context_pack_orchestrator import ModelContextPackCommand

from .test_compaction_pack_build import _pack_orchestrator, _request, _sources

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "context compaction fell back" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_compaction_pack_build_bus_one_command_then_built(compaction_bus):
    bus = compaction_bus(_pack_orchestrator(_sources()))
    request = _request()
    output = await bus.handler().handle_async(request)
    reply = output.events[-1]
    assert reply.status == "built"
    assert reply.package_id == output.events[0].candidate_id
    (envelope,) = bus.commands
    ((cid, _),) = bus.consumer.waits
    assert envelope["correlation_id"] == cid
    assert cid != str(request.invocation_id)
    command = ModelContextPackCommand.model_validate(envelope["payload"])
    assert command.request.profile_name == "compaction_orchestrator"
    assert command.request.session_id == request.session_id
    assert command.request.session_barrier == request.barrier_id
    assert [topic for topic, _ in bus.records] == [
        bus.topics.request_topic,
        bus.topics.completed_topic,
    ]
    assert bus.records[-1][1]["payload"]["candidate_id"] == str(reply.package_id)
    assert bus.records[-1][1]["correlation_id"] == cid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raises", [False, True], ids=["failed-result", "raised-handler"]
)
async def test_compaction_pack_build_bus_failed_reply_is_native(
    compaction_bus, caplog, raises
):
    class RaisingPack:
        def handle(self, command):
            raise RuntimeError("pack source failed")

    pack = (
        RaisingPack()
        if raises
        else _pack_orchestrator(_sources(open_work=EnumContextSourceStatus.UNAVAILABLE))
    )
    bus = compaction_bus(pack)
    output = await bus.handler().handle_async(_request())
    assert output.events[-1].status == "native"
    assert output.events[0].failure_class.value == "pack_build_failed"
    assert [topic for topic, _ in bus.records] == [
        bus.topics.request_topic,
        bus.topics.failed_topic,
    ]
    if raises:
        assert bus.records[-1][1]["payload_type"] == "ModelBoundaryFailureTerminal"
        assert bus.records[-1][1]["payload"]["failure_class"] == "RuntimeError"
    else:
        failed = bus.records[-1][1]["payload"]
        assert failed["status"] == "failed"
        assert failed["terminal_failure_cause"] is not None
        assert failed["terminal_failure_cause"] == failed["failure_class"]
    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_compaction_pack_build_bus_deadline_falls_back_once(
    compaction_bus, caplog
):
    bus = compaction_bus(silent=True)
    from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
        load_compaction_runtime_binding,
    )

    binding = load_compaction_runtime_binding().model_copy(
        update={"tenant_id": "tenant-a", "barrier_deadline_s": 0.03}
    )
    handler = bus.handler(binding=binding)
    request = _request()
    output = await handler.handle_async(request)
    assert output.events[-1].status == "native"
    assert output.events[0].failure_class.value == "pack_builder_unavailable"
    assert len(bus.commands) == 1
    (warning,) = _warnings(caplog)
    assert str(request.invocation_id) in warning.getMessage()


def test_compaction_pack_build_bus_no_in_process_pack_handler():
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_context_compaction_orchestrator"
    )
    for path in root.rglob("*.py"):
        source = path.read_text()
        assert "omnimarket.nodes.node_context_pack_orchestrator" not in source, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert ast.unparse(node.func) != "importlib.import_module", path


@pytest.mark.asyncio
async def test_mismatched_terminal_correlation_is_build_error(compaction_bus, caplog):
    from uuid import uuid4

    bus = compaction_bus(silent=True)

    def publish(topic, payload):
        import json

        command = json.loads(payload)
        bus.consumer.pending[command["correlation_id"]].set_result(
            {
                "correlation_id": str(uuid4()),
                "payload": {"status": "failed"},
            }
        )

    output = await bus.handler(event_publisher=publish).handle_async(_request())
    assert output.events[0].failure_class.value == "build_error"
    (warning,) = _warnings(caplog)
    assert "cause=ValueError" in warning.getMessage()


@pytest.mark.asyncio
async def test_revision_probe_consumes_the_remaining_compaction_deadline(
    compaction_bus,
):
    from .test_compaction_pack_build_barrier import _binding, _Clock, _probe
    from .test_session_barrier_wait import _request as barrier_request

    clock = _Clock()

    class Projection:
        def __init__(self):
            self.reads = 0

        def read(self, session_id, barrier_id):
            self.reads += 1
            if self.reads == 2:
                clock.sleep(2)
            return _probe((1, 2, 3), (1, 2, 3))

    bus = compaction_bus(silent=True)
    output = await bus.handler(
        binding=_binding(), barrier_probe_source=Projection(), monotonic=clock.monotonic
    ).handle_async(barrier_request())
    assert output.events[0].failure_class.value == "pack_builder_unavailable"
    assert not bus.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision_missing", [True, False], ids=["no-revision", "no-longer-met"]
)
async def test_revision_probe_must_still_prove_completeness(
    compaction_bus, revision_missing
):
    from .test_compaction_pack_build_barrier import _probe
    from .test_session_barrier_wait import _request as barrier_request
    from .test_session_barrier_wait import _Source

    met = _probe((1, 2, 3), (1, 2, 3))
    second = (
        met.model_copy(update={"source_revision": None})
        if revision_missing
        else _probe((1, 3), (1, 2, 3))
    )
    bus = compaction_bus(silent=True)
    output = await bus.handler(
        barrier_probe_source=_Source([met, second])
    ).handle_async(barrier_request())
    assert output.events[0].failure_class.value == (
        "source_incomplete" if revision_missing else "source_not_caught_up"
    )
    assert not bus.commands
