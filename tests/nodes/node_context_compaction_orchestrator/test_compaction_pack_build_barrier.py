# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-bound pack builds wait for the session barrier (OMN-21334, TA.11b)."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Literal

import pytest
import yaml
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionRequested,
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
    compaction_runtime_binding as runtime_binding,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers import (
    handler_context_compaction_orchestrator as handler_module,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
    ModelCompactionRuntimeBinding,
    bind_compaction_ports,
    load_compaction_runtime_binding,
    resolve_pack_orchestrator,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_compaction_pack_build import (
    HandlerCompactionPackBuild,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_session_barrier_wait import (
    HandlerSessionBarrierWait,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
    ModelSessionBarrierReadBinding,
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


class _RaisingBuild:
    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome:
        raise RuntimeError("pack build failed")


def _binding() -> ModelCompactionRuntimeBinding:
    return ModelCompactionRuntimeBinding(
        pack_build_node="node_context_pack_orchestrator",
        tenant_id="lab",
        barrier_deadline_s=1.25,
        barrier_poll_interval_s=0.5,
        barrier_probe=ModelSessionBarrierReadBinding(
            dsn_env="OMNINODE_INTERNAL_DB_URL",
            lineage_relation="omninode_internal.claude_hook_events",
            content_relation="omninode_internal.session_content",
        ),
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == handler_module.__name__ and record.levelno == logging.WARNING
    ]


def test_compaction_pack_build_barrier_no_arg_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    binding = load_compaction_runtime_binding()
    assert binding.pack_build_node == "node_context_pack_orchestrator"
    assert binding.tenant_id == "lab"
    assert binding.barrier_deadline_s == 30.0
    assert binding.barrier_poll_interval_s == 0.5

    monkeypatch.delenv("ONEX_CONTEXT_SOURCE_TENANT_ID")
    assert load_compaction_runtime_binding().tenant_id is None
    result = HandlerContextCompactionOrchestrator().handle(_request(barrier_id=None))
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
    )


def test_compaction_pack_build_barrier_waits_then_builds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Source(
        [
            _probe((1, 3), (1, 2, 3)),
            _probe((1, 3), (1, 2, 3)),
            _probe((1, 2, 3), (1, 2, 3)),
        ]
    )
    stub = _PackOrchestrator()
    clock = _Clock()
    pack_build, barrier_wait = bind_compaction_ports(
        _binding(),
        barrier_probe_source=source,
        resolve=lambda _name: stub,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert pack_build is not None
    assert barrier_wait is not None
    with caplog.at_level(logging.WARNING, logger=handler_module.__name__):
        result = HandlerContextCompactionOrchestrator(
            pack_build, barrier_wait=barrier_wait
        ).handle(_request())
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert result.prepared is not None
    assert result.activated is not None
    assert result.fallback is None
    assert result.reply.status == "built"
    assert result.reply.package_id == result.prepared.candidate_id
    assert source.reads == [("session-1", BARRIER)] * 4
    assert clock.sleeps == [0.5, 0.5]
    (command,) = stub.commands
    assert command.request.profile_name == "compaction_orchestrator"
    assert command.request.session_barrier == BARRIER
    assert command.access.tenant_id == "lab"
    assert _warnings(caplog) == []


def test_compaction_pack_build_barrier_deadline_falls_back_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Source([_probe((1, 3), (1, 2, 3))])
    stub = _PackOrchestrator()
    clock = _Clock()
    binding = _binding()
    pack_build, barrier_wait = bind_compaction_ports(
        binding,
        barrier_probe_source=source,
        resolve=lambda _name: stub,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    request = _request()
    with caplog.at_level(logging.WARNING, logger=handler_module.__name__):
        result = HandlerContextCompactionOrchestrator(
            pack_build, barrier_wait=barrier_wait
        ).handle(request)
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.reply.status == "native"
    assert result.reply.package_id is None
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert stub.commands == []
    assert clock.monotonic() == binding.barrier_deadline_s
    (warning,) = _warnings(caplog)
    assert str(request.invocation_id) in warning.getMessage()
    assert "source_not_caught_up" in warning.getMessage()
    assert warning.exc_info is None


def test_compaction_pack_build_barrier_no_arg_resolves_lazily_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    stub = _PackOrchestrator()
    resolutions: list[str] = []

    def resolve(node_name: str) -> _PackOrchestrator:
        resolutions.append(node_name)
        return stub

    monkeypatch.setattr(runtime_binding, "resolve_pack_orchestrator", resolve)
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    handler = HandlerContextCompactionOrchestrator(barrier_probe_source=source)
    assert resolutions == []
    assert stub.commands == []
    for _ in range(2):
        result = handler.handle(_request())
        assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert resolutions == ["node_context_pack_orchestrator"]
    assert len(stub.commands) == 2
    assert all(command.access.tenant_id == "lab" for command in stub.commands)


def test_compaction_pack_build_barrier_real_contract_resolves() -> None:
    handler = resolve_pack_orchestrator("node_context_pack_orchestrator")
    assert type(handler).__name__ == "HandlerContextPackOrchestrator"
    assert callable(handler.handle)


def test_compaction_pack_build_barrier_uses_recorded_source_revisions() -> None:
    stub = _PackOrchestrator()
    result = HandlerCompactionPackBuild(
        stub, access=ModelContextAccess(tenant_id="lab")
    ).build(_request(), source_revision="2026-10-23T12:00:00.500000Z")
    assert result.package is not None
    (command,) = stub.commands
    assert command.request.session_barrier == BARRIER
    assert command.request.tier_revisions == {
        EnumContextFactor.OPERATOR_RULINGS: "2026-10-23T12:00:00Z",
        EnumContextFactor.OPEN_WORK: "2026-10-23T12:00:00Z",
        EnumContextFactor.ORCHESTRATION_STATE: "2026-10-23T12:00:00Z",
        EnumContextFactor.OPERATOR_WORDS_UNRECORDED: "2026-10-23T12:00:00.500000Z",
    }


def test_compaction_pack_build_barrier_no_default_bypass(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    stub = _PackOrchestrator()
    monkeypatch.setattr(runtime_binding, "resolve_pack_orchestrator", lambda _: stub)
    handler = HandlerContextCompactionOrchestrator()
    assert handler._barrier_wait is not None
    result = handler.handle(_request())
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert stub.commands == []
    assert len(_warnings(caplog)) == 1


def test_compaction_pack_build_barrier_missing_barrier_never_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    result = HandlerContextCompactionOrchestrator().handle(_request(barrier_id=None))
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
    )


def test_compaction_pack_build_barrier_late_met_does_not_build(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()

    class SlowProjection:
        def read(
            self, session_id: str, barrier_id: str
        ) -> ModelSessionStreamBarrierProbe:
            clock.sleep(2)
            return _probe((1, 2, 3), (1, 2, 3))

    stub = _PackOrchestrator()
    wait = HandlerSessionBarrierWait(
        SlowProjection(),
        deadline_s=1,
        poll_interval_s=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = HandlerContextCompactionOrchestrator(
        HandlerCompactionPackBuild(stub, access=ModelContextAccess(tenant_id="lab")),
        barrier_wait=wait,
    ).handle(_request())
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert stub.commands == []
    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_compaction_pack_build_barrier_async_wait_leaves_projection_running() -> (
    None
):
    applied = threading.Event()

    class Projection:
        def read(
            self, session_id: str, barrier_id: str
        ) -> ModelSessionStreamBarrierProbe:
            return _probe((1, 2, 3) if applied.is_set() else (1, 3), (1, 2, 3))

    stub = _PackOrchestrator()
    pack, wait = bind_compaction_ports(
        _binding().model_copy(
            update={"barrier_deadline_s": 1.0, "barrier_poll_interval_s": 0.005}
        ),
        barrier_probe_source=Projection(),
        resolve=lambda _: stub,
    )
    handler = HandlerContextCompactionOrchestrator(pack, barrier_wait=wait)

    async def project_missing_record() -> None:
        await asyncio.sleep(0.02)
        applied.set()

    output, _ = await asyncio.gather(
        handler.handle_async(_request()), project_missing_record()
    )
    reply = output.events[-1]
    assert reply.status == "built"
    assert len(stub.commands) == 1


def test_compaction_pack_build_barrier_raising_build_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(barrier_id=None)
    with caplog.at_level(logging.WARNING, logger=handler_module.__name__):
        result = HandlerContextCompactionOrchestrator(_RaisingBuild()).handle(request)
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.fallback is not None
    assert (
        result.fallback.failure_class == EnumContextCompactionFailureClass.BUILD_ERROR
    )
    (warning,) = _warnings(caplog)
    assert "build_error" in warning.getMessage()
    assert "cause=RuntimeError" in warning.getMessage()
    assert str(request.invocation_id) in warning.getMessage()
    assert warning.exc_info is None
    assert "\n" not in warning.getMessage()


def test_compaction_pack_build_barrier_constructor_binds_probe_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    stub = _PackOrchestrator()
    monkeypatch.setattr(
        runtime_binding, "resolve_pack_orchestrator", lambda _name: stub
    )
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    result = HandlerContextCompactionOrchestrator(barrier_probe_source=source).handle(
        _request()
    )
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert source.reads == [("session-1", BARRIER)] * 2
    assert len(stub.commands) == 1


def test_compaction_pack_build_barrier_constructor_keeps_explicit_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "lab")
    stub = _PackOrchestrator()
    monkeypatch.setattr(
        runtime_binding, "resolve_pack_orchestrator", lambda _name: stub
    )
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    _, barrier_wait = bind_compaction_ports(_binding(), barrier_probe_source=source)
    unused_source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    result = HandlerContextCompactionOrchestrator(
        barrier_wait=barrier_wait, barrier_probe_source=unused_source
    ).handle(_request())
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert source.reads == [("session-1", BARRIER)]
    assert unused_source.reads == [("session-1", BARRIER)]
    assert len(stub.commands) == 1


def test_compaction_pack_build_barrier_explicit_build_skips_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _PackOrchestrator()
    pack_build = HandlerCompactionPackBuild(
        stub, access=ModelContextAccess(tenant_id="lab")
    )

    def unexpected_binding(**kwargs: object) -> None:
        pytest.fail("an explicit pack build must not load the runtime binding")

    monkeypatch.setattr(handler_module, "bind_compaction_ports", unexpected_binding)
    source = _Source([_probe((1,), (1,))])
    result = HandlerContextCompactionOrchestrator(
        pack_build, barrier_probe_source=source
    ).handle(_request())
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert source.reads == []
    assert len(stub.commands) == 1


@pytest.mark.parametrize("tenant", ["", "   ", " lab "])
def test_compaction_pack_build_barrier_tenant_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
    tenant: str,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", tenant)
    assert load_compaction_runtime_binding().tenant_id == (tenant.strip() or None)


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        {},
        {
            "pack_build_node": "node_context_pack_orchestrator",
            "tenant_id": "lab",
            "barrier_wait": [],
        },
        {
            "pack_build_node": "node_context_pack_orchestrator",
            "tenant_id": 1,
            "barrier_wait": {"deadline_s": 1.0, "poll_interval_s": 0.5},
        },
        {
            "pack_build_node": "node_context_pack_orchestrator",
            "tenant_id": "lab",
            "barrier_wait": {"deadline_s": 0.0, "poll_interval_s": 0.5},
        },
        {
            "pack_build_node": "node_context_pack_orchestrator",
            "tenant_id": "lab",
            "barrier_wait": {"deadline_s": 1.0, "poll_interval_s": -0.5},
        },
    ],
)
def test_compaction_pack_build_barrier_malformed_binding(
    tmp_path: Path,
    block: object,
) -> None:
    if isinstance(block, dict) and isinstance(block.get("barrier_wait"), dict):
        block["barrier_wait"]["probe"] = _binding().barrier_probe.model_dump()
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"compaction_runtime": block}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"compaction_runtime|greater than 0"):
        load_compaction_runtime_binding(path)
