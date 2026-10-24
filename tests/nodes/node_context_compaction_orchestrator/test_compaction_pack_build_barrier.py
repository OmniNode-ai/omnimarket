# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-bound pack builds wait for the session barrier (OMN-21334, TA.11b)."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Literal

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierProbe,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers import (
    handler_context_compaction_orchestrator as handler_module,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
    ModelCompactionRuntimeBinding,
    bind_compaction_ports,
    load_compaction_pack_topics,
    load_compaction_runtime_binding,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_compaction_pack_build import (
    build_pack_command,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)

from .test_session_barrier_wait import BARRIER, _request, _Source
from .test_session_barrier_wait import _probe as _base_probe


def _probe(
    lineage_applied: tuple[int, ...],
    content_applied: tuple[int, ...],
    *,
    content_capture: Literal["enabled", "disabled"] | None = None,
) -> ModelSessionStreamBarrierProbe:
    return _base_probe(
        lineage_applied, content_applied, content_capture=content_capture
    ).model_copy(update={"source_revision": "2026-10-23T12:00:00Z"})


pytestmark = pytest.mark.unit


class _PackOrchestrator:
    """Record commands and return one complete built package."""

    def __init__(self) -> None:
        self.commands: list[ModelContextPackCommand] = []

    def handle(self, command: ModelContextPackCommand) -> ModelContextPackBuilt:
        self.commands.append(command)
        return ModelContextPackBuilt(
            status=EnumContextPackOrchestratorStatus.OK,
            candidate_id="22222222-2222-5222-8222-222222222222",
            profile_name="compaction_orchestrator",
            profile_content_hash="a" * 64,
            version_vector_hash="b" * 64,
            body_hash="c" * 64,
            final_pack_hash="d" * 64,
            assembly_hash="e" * 64,
            messages=(ModelContextPackRenderMessage(role="user", text="pack"),),
        )


class _Clock:
    """Advance a list-backed monotonic clock on every recorded sleep."""

    def __init__(self) -> None:
        self.now = [0.0]
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now[0]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now[0] += seconds


def _binding() -> ModelCompactionRuntimeBinding:
    return load_compaction_runtime_binding().model_copy(
        update={
            "tenant_id": "lab",
            "barrier_deadline_s": 1.25,
            "barrier_poll_interval_s": 0.5,
        }
    )


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == handler_module.__name__ and r.levelno == logging.WARNING
    ]


def _handler(compaction_bus, source=None, clock=None, **kwargs):
    source = source if source is not None else _Source([_probe((1, 2, 3), (1, 2, 3))])
    binding = kwargs.pop("binding", _binding())
    if clock is not None:
        wait, _ = bind_compaction_ports(
            binding,
            barrier_probe_source=source,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        kwargs.setdefault("barrier_wait", wait)
        kwargs["monotonic"] = clock.monotonic
    bus = compaction_bus(_PackOrchestrator())
    return bus, bus.handler(binding=binding, barrier_probe_source=source, **kwargs)


def test_compaction_pack_build_barrier_no_arg_binding(monkeypatch):
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    binding = load_compaction_runtime_binding()
    assert binding.tenant_id == "lab"
    assert binding.barrier_deadline_s == 30.0
    assert binding.barrier_poll_interval_s == 0.5
    monkeypatch.delenv("ONEX_CONTEXT_SOURCE_TENANT_ID")
    assert load_compaction_runtime_binding().tenant_id is None
    result = HandlerContextCompactionOrchestrator().handle(_request(barrier_id=None))
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
    )


def test_compaction_pack_build_barrier_waits_then_builds(compaction_bus, caplog):
    source = _Source(
        [
            _probe((1, 3), (1, 2, 3)),
            _probe((1, 3), (1, 2, 3)),
            _probe((1, 2, 3), (1, 2, 3)),
        ]
    )
    clock = _Clock()
    bus, handler = _handler(compaction_bus, source, clock)
    result = handler.handle(_request())
    assert result.reply.status == "built"
    assert result.reply.package_id == result.prepared.candidate_id
    assert result.activated is not None
    assert result.fallback is None
    assert source.reads == [("session-1", BARRIER)] * 4
    assert clock.sleeps == [0.5, 0.5]
    (command,) = bus.pack.commands
    assert command.request.profile_name == "compaction_orchestrator"
    assert command.request.session_barrier == BARRIER
    assert command.access.tenant_id == "lab"
    assert bus.consumer.waits[0][1] == 0.25
    assert _warnings(caplog) == []


def test_compaction_pack_build_barrier_deadline_falls_back_once(compaction_bus, caplog):
    source = _Source([_probe((1, 3), (1, 2, 3))])
    clock = _Clock()
    bus, handler = _handler(compaction_bus, source, clock)
    request = _request()
    result = handler.handle(request)
    assert result.reply.status == "native"
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert not bus.commands
    assert clock.monotonic() == _binding().barrier_deadline_s
    (warning,) = _warnings(caplog)
    assert str(request.invocation_id) in warning.getMessage()
    assert "source_not_caught_up" in warning.getMessage()
    assert warning.exc_info is None


def test_compaction_pack_build_barrier_no_arg_resolves_lazily_once(
    compaction_bus, monkeypatch
):
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    bus = compaction_bus(_PackOrchestrator())
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    handler = HandlerContextCompactionOrchestrator(
        event_publisher=bus.publisher,
        event_consumer=bus.consumer,
        barrier_probe_source=source,
    )
    assert not bus.commands
    for _ in range(2):
        assert handler.handle(_request()).reply.status == "built"
    assert len(bus.commands) == 2
    assert {topic for topic, _ in bus.consumer.registrations} == {
        bus.topics.completed_topic,
        bus.topics.failed_topic,
    }
    assert all(c.access.tenant_id == "lab" for c in bus.pack.commands)
    assert bus.commands[0]["correlation_id"] != bus.commands[1]["correlation_id"]


def test_compaction_pack_build_barrier_real_contract_resolves():
    topics = load_compaction_pack_topics()
    assert topics.request_topic == "onex.cmd.omniintelligence.context-pack-requested.v1"
    assert topics.completed_topic == "onex.cmd.omniintelligence.context-pack-built.v1"
    assert topics.failed_topic == "onex.cmd.omniintelligence.context-pack-failed.v1"


def test_compaction_pack_build_barrier_uses_recorded_source_revisions():
    command = build_pack_command(
        _request(), ModelContextAccess(tenant_id="lab"), "2026-10-23T12:00:00.500000Z"
    )
    assert command.request.session_barrier == BARRIER
    assert command.request.tier_revisions == {
        EnumContextFactor.OPERATOR_RULINGS: "2026-10-23T12:00:00Z",
        EnumContextFactor.OPEN_WORK: "2026-10-23T12:00:00Z",
        EnumContextFactor.ORCHESTRATION_STATE: "2026-10-23T12:00:00Z",
        EnumContextFactor.OPERATOR_WORDS_UNRECORDED: "2026-10-23T12:00:00.500000Z",
    }


def test_compaction_pack_build_barrier_no_default_bypass(
    compaction_bus, monkeypatch, caplog
):
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    bus = compaction_bus(_PackOrchestrator())
    handler = HandlerContextCompactionOrchestrator(
        bus.publisher, bus.consumer, binding=_binding()
    )
    assert handler._barrier_wait is not None
    result = handler.handle(_request())
    assert result.reply.status == "native"
    assert not bus.commands
    assert len(_warnings(caplog)) == 1


def test_compaction_pack_build_barrier_missing_barrier_never_builds(compaction_bus):
    bus, handler = _handler(compaction_bus)
    result = handler.handle(_request(barrier_id=None))
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
    )
    assert not bus.commands


def test_compaction_pack_build_barrier_late_met_does_not_build(compaction_bus, caplog):
    clock = _Clock()

    class SlowProjection:
        def read(self, session_id, barrier_id):
            clock.sleep(2)
            return _probe((1, 2, 3), (1, 2, 3))

    bus, handler = _handler(compaction_bus, SlowProjection(), clock)
    result = handler.handle(_request())
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert not bus.commands
    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_compaction_pack_build_barrier_async_wait_leaves_projection_running(
    compaction_bus,
):
    applied = threading.Event()

    class Projection:
        def read(self, session_id, barrier_id):
            return _probe((1, 2, 3) if applied.is_set() else (1, 3), (1, 2, 3))

    bus, handler = _handler(
        compaction_bus,
        Projection(),
        binding=_binding().model_copy(
            update={"barrier_deadline_s": 1.0, "barrier_poll_interval_s": 0.005}
        ),
    )

    async def project_missing_record():
        await asyncio.sleep(0.02)
        applied.set()

    output, _ = await asyncio.gather(
        handler.handle_async(_request()), project_missing_record()
    )
    assert output.events[-1].status == "built"
    assert len(bus.commands) == 1


def test_compaction_pack_build_barrier_raising_build_logs_once(compaction_bus, caplog):
    bus, handler = _handler(compaction_bus)

    def raising_publisher(topic, payload):
        raise RuntimeError("pack publish failed")

    handler = bus.handler(event_publisher=raising_publisher)
    request = _request()
    result = handler.handle(request)
    assert (
        result.fallback.failure_class == EnumContextCompactionFailureClass.BUILD_ERROR
    )
    (warning,) = _warnings(caplog)
    assert "cause=RuntimeError" in warning.getMessage()
    assert str(request.invocation_id) in warning.getMessage()
    assert warning.exc_info is None
    assert "\n" not in warning.getMessage()


def test_compaction_pack_build_barrier_constructor_binds_probe_source(compaction_bus):
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    bus, handler = _handler(compaction_bus, source)
    assert handler.handle(_request()).reply.status == "built"
    assert source.reads == [("session-1", BARRIER)] * 2
    assert len(bus.commands) == 1


def test_compaction_pack_build_barrier_constructor_keeps_explicit_wait(compaction_bus):
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    wait, _ = bind_compaction_ports(_binding(), barrier_probe_source=source)
    revision_source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    bus, handler = _handler(compaction_bus, revision_source, barrier_wait=wait)
    assert handler.handle(_request()).reply.status == "built"
    assert source.reads == revision_source.reads == [("session-1", BARRIER)]
    assert len(bus.commands) == 1


def test_compaction_pack_build_barrier_explicit_build_skips_runtime_binding(
    compaction_bus, monkeypatch
):
    def unexpected_binding():
        pytest.fail("explicit binding must not reload runtime settings")

    binding = _binding()
    monkeypatch.setattr(
        handler_module, "load_compaction_runtime_binding", unexpected_binding
    )
    bus, handler = _handler(compaction_bus, binding=binding)
    assert handler.handle(_request()).reply.status == "built"
    assert len(bus.commands) == 1


@pytest.mark.parametrize("tenant", ["", "   ", " lab "])
def test_compaction_pack_build_barrier_tenant_is_stripped(monkeypatch, tenant):
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", tenant)
    assert load_compaction_runtime_binding().tenant_id == (tenant.strip() or None)


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        {},
        {"tenant_id": "lab", "barrier_wait": []},
        {"tenant_id": 1, "barrier_wait": {"deadline_s": 1.0, "poll_interval_s": 0.5}},
        {
            "tenant_id": "lab",
            "barrier_wait": {"deadline_s": 0.0, "poll_interval_s": 0.5},
        },
        {
            "tenant_id": "lab",
            "barrier_wait": {"deadline_s": 1.0, "poll_interval_s": -0.5},
        },
    ],
)
def test_compaction_pack_build_barrier_malformed_binding(tmp_path, block):
    if isinstance(block, dict) and isinstance(block.get("barrier_wait"), dict):
        block["barrier_wait"]["probe"] = _binding().barrier_probe.model_dump()
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"compaction_runtime": block}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"compaction_runtime|greater than 0"):
        load_compaction_runtime_binding(path)
