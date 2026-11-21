# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Selected Docker requirements via controlled process transport, never Docker.

Catch a running-but-unhealthy stack, declared reservations above engine capacity,
terms/permission refusal, an omitted selected service, and secret-bearing process
errors. Tiny stacks below old universal floors remain allowed. Artifact records
only commands and typed outcomes, never the injected environment/diagnostic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.e2e


def _plan(tmp_path: Path) -> ModelSetupPlan:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {worker: {image: user-selected}}\n")
    topology = tmp_path / "topology.yaml"
    topology.write_text(
        "schema_version: 1.0.0\nservices:\n  worker:\n    mode: LOCAL\n    local: {compose_service: worker, host_port: 38123}\n"
    )
    return ModelSetupPlan(
        workspace=tmp_path / "workspace",
        components=("local-stack",),
        local_stack={"compose_file": compose, "topology_file": topology},
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("tiny", "success"),
        ("roomy", "success"),
        ("memory", "action_required"),
        ("cpu", "action_required"),
        ("terms", "action_required"),
        ("unhealthy", "unavailable"),
        ("missing", "unavailable"),
        ("bad-json", "failure"),
    ],
)
def test_selected_stack_requirements_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )

    commands = []
    sentinel = "planted-stack-private-diagnostic"
    info = {"NCPU": 1, "MemTotal": 256 * 1024 * 1024, "OSType": "linux"}
    if case == "roomy":
        # Both above the tiny engine, so a reader that drops real values fails here.
        info = {"NCPU": 4, "MemTotal": 1024 * 1024 * 1024, "OSType": "linux"}
    engine_timeouts: list[float] = []

    async def read_engine(timeout_seconds: float) -> tuple[int, str, str]:
        engine_timeouts.append(timeout_seconds)
        if case == "terms":
            return 1, "accept terms " + sentinel, ""
        return 0, "", "bad " + sentinel if case == "bad-json" else json.dumps(info)

    monkeypatch.setattr(
        "omnibase_infra.handlers.handler_local_engine_info._read_engine", read_engine
    )
    services = {
        "worker": {
            "deploy": {
                "resources": {
                    "reservations": {"cpus": "0.1", "memory": 32 * 1024 * 1024}
                }
            },
            "environment": {"SECRET": sentinel},
            "healthcheck": {"test": ["CMD", "true"]},
        }
    }
    if case == "memory":
        services["worker"]["deploy"]["resources"]["reservations"]["memory"] = (
            512 * 1024 * 1024
        )
    if case == "cpu":
        services["worker"]["deploy"]["resources"]["reservations"]["cpus"] = "2"
    if case == "roomy":
        services["worker"]["deploy"]["resources"]["reservations"] = {
            "cpus": "2",
            "memory": 512 * 1024 * 1024,
        }
    rows = (
        []
        if case == "missing"
        else [
            {
                "Service": "worker",
                "State": "running",
                "Health": "unhealthy" if case == "unhealthy" else "healthy",
            }
        ]
    )

    def transport(self, request):
        commands.append(list(request.argv))
        if "config" in request.argv:
            raw = json.dumps({"services": services})
        elif "ps" in request.argv:
            raw = "\n".join(json.dumps(row) for row in rows)
        else:
            raise AssertionError(request.argv)
        return ModelSetupCommandResult(
            argv=request.argv, returncode=0, stdout=raw, stderr=""
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", transport)
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_stack_requirements import (
        ModelSetupStackRequirementsRequest,
    )

    request = ModelSetupStackRequirementsRequest(
        plan=_plan(tmp_path), services=("worker",)
    )
    result = asyncio.run(HandlerSetupStackRequirements().handle(request))
    assert result.status == expected
    if expected == "success":
        assert result.evidence["engine_cpus"] == str(float(info["NCPU"]))
        assert result.evidence["engine_memory_bytes"] == str(info["MemTotal"])
    from uuid import uuid4

    from omnibase_infra.nodes.node_setup_local_provision_effect.models.model_local_provision_effect_output import (
        ModelLocalProvisionEffectOutput,
    )

    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_local_stack import (
        HandlerSetupLocalStack,
    )

    async def running(_handler, _envelope):
        return ModelLocalProvisionEffectOutput(
            success=True, correlation_id=uuid4(), services_running=("worker",)
        )

    owner = HandlerSetupLocalStack(onex_home=tmp_path / "onex")
    plan = _plan(tmp_path)
    owner._applied_digest = owner._inputs(plan)[2]
    monkeypatch.setattr(owner, "_execute", running)
    monkeypatch.setattr("shutil.which", lambda _name: "/fixture/docker")
    integrated = owner.observe(plan)
    assert integrated.status == expected, (
        "running services must not hide resource or health failures"
    )
    assert sentinel not in result.model_dump_json()
    assert sentinel not in integrated.model_dump_json()
    assert engine_timeouts == [20, 20]
    assert not any("up" in cmd or "restart" in cmd for cmd in commands)
    assert not (tmp_path / "workspace").exists()
    if case == "tiny":
        from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
            ProtocolLocalRuntimeBus,
        )
        from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

        bus = AsyncMock(spec=ProtocolLocalRuntimeBus)
        adapter = LocalRuntimeBusAdapter(
            handler=HandlerSetupStackRequirements(),
            handler_name="onex_setup.stack_requirements",
            input_model_cls=ModelSetupStackRequirementsRequest,
            output_topic="observations",
            bus=bus,
        )
        asyncio.run(
            adapter.on_message(
                SimpleNamespace(value=request.model_dump_json().encode())
            )
        )
        bus.publish.assert_awaited_once_with(
            "observations", None, result.model_dump_json().encode()
        )
        assert engine_timeouts == [20, 20, 20]
        assert result.evidence["resource_basis"] == "selected service reservations only"
        assert result.evidence["engine_cpus"] == "1.0"
        assert result.evidence["engine_memory_bytes"] == str(256 * 1024 * 1024)
    (tmp_path / "stack-requirements-evidence.json").write_text(
        json.dumps(
            {
                "proof_kind": "injected process transport; no Docker engine",
                "commands": commands,
                "result": result.model_dump(mode="json"),
            },
            indent=2,
        )
    )


def test_selected_stack_with_older_infra_reports_install_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sys.modules, "omnibase_infra.handlers.handler_local_engine_info", None
    )
    monkeypatch.delitem(
        sys.modules,
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements",
        raising=False,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )

    def unexpected_command(self, request):
        pytest.fail(f"Older infra must refuse before executing {request.argv}")

    monkeypatch.setattr(HandlerSetupCommand, "handle", unexpected_command)
    result = HandlerSetupStackRequirements().observe(_plan(tmp_path), ("worker",))
    assert result.status == "action_required"
    assert "omnibase-infra" in result.fix
    assert "upgrade" in result.fix.lower()


def test_selected_stack_reads_the_engine_through_the_injected_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )

    calls: list[str] = []

    async def unavailable_engine() -> tuple[float, int]:
        calls.append("read")
        raise OSError("Docker observation unavailable")

    def unexpected_command(self, request) -> None:
        pytest.fail(f"An unavailable engine must stop before {request.argv}")

    monkeypatch.setattr(HandlerSetupCommand, "handle", unexpected_command)
    result = HandlerSetupStackRequirements(engine_reader=unavailable_engine).observe(
        _plan(tmp_path), ("worker",)
    )
    assert calls == ["read"]
    assert result.status == "action_required"


@pytest.mark.parametrize(
    ("reserved_cpus", "engine"),
    [
        ("8", (float("nan"), 200)),
        ("0", (0.0, 0)),
        ("8", (float("inf"), 1 << 30)),
    ],
)
def test_selected_stack_refuses_invalid_capacity_from_an_injected_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved_cpus: str,
    engine: tuple[float, int],
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )

    commands: list[list[str]] = []

    async def reader() -> tuple[float, int]:
        return engine

    def transport(self, request):
        commands.append(list(request.argv))
        reservations = {"cpus": reserved_cpus}
        if "config" in request.argv:
            body = {
                "services": {
                    "worker": {"deploy": {"resources": {"reservations": reservations}}}
                }
            }
        else:
            body = {"Service": "worker", "State": "running", "Health": ""}
        return ModelSetupCommandResult(
            argv=request.argv, returncode=0, stdout=json.dumps(body), stderr=""
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", transport)
    result = HandlerSetupStackRequirements(engine_reader=reader).observe(
        _plan(tmp_path), ("worker",)
    )
    assert result.status == "failure", (
        "a non-finite or non-positive engine capacity must never compare as enough"
    )
    assert commands == [], "the engine answer is refused before any Compose command"


def test_selected_stack_bounds_an_injected_reader_by_the_engine_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_stack_requirements as module,
    )

    # The deadline is shortened so a reader that would answer after it stays fast.
    monkeypatch.setattr(module, "_ENGINE_READ_TIMEOUT_SECONDS", 0.2, raising=False)

    async def stalled_engine() -> tuple[float, int]:
        await asyncio.sleep(3)
        return 4.0, 1 << 30

    def unexpected_command(self, request) -> None:
        pytest.fail(f"An engine past its deadline must stop before {request.argv}")

    monkeypatch.setattr(HandlerSetupCommand, "handle", unexpected_command)
    started = time.monotonic()
    result = module.HandlerSetupStackRequirements(engine_reader=stalled_engine).observe(
        _plan(tmp_path), ("worker",)
    )
    elapsed = time.monotonic() - started
    assert result.status == "action_required"
    assert elapsed < 2, f"the reader ran {elapsed:.2f}s past its deadline"


def test_bus_run_reads_an_injected_reader_on_the_bus_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
        ProtocolLocalRuntimeBus,
    )
    from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
        HandlerSetupStackRequirements,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_stack_requirements import (
        ModelSetupStackRequirementsRequest,
    )

    def transport(self, request):
        if "config" in request.argv:
            body = {"services": {"worker": {}}}
        else:
            body = {"Service": "worker", "State": "running", "Health": ""}
        return ModelSetupCommandResult(
            argv=request.argv, returncode=0, stdout=json.dumps(body), stderr=""
        )

    monkeypatch.setattr(HandlerSetupCommand, "handle", transport)
    bus = AsyncMock(spec=ProtocolLocalRuntimeBus)
    errors: list[str] = []
    request = ModelSetupStackRequirementsRequest(
        plan=_plan(tmp_path), services=("worker",)
    )

    async def run_on_bus() -> None:
        # A reader sharing the runtime's loop: its answer is a future of that loop.
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[tuple[float, int]] = loop.create_future()
        loop.call_later(0.05, answer.set_result, (4.0, 1 << 30))

        async def bus_loop_reader() -> tuple[float, int]:
            return await answer

        adapter = LocalRuntimeBusAdapter(
            handler=HandlerSetupStackRequirements(engine_reader=bus_loop_reader),
            handler_name="onex_setup.stack_requirements",
            input_model_cls=ModelSetupStackRequirementsRequest,
            output_topic="observations",
            bus=bus,
            on_error=lambda: errors.append("handler raised"),
        )
        await adapter.on_message(
            SimpleNamespace(value=request.model_dump_json().encode())
        )

    asyncio.run(run_on_bus())
    assert errors == [], "the reader must run on the loop that owns its answer"
    bus.publish.assert_awaited_once()
    published = json.loads(bus.publish.await_args.args[2])
    assert published["status"] == "success"
    assert published["evidence"]["engine_cpus"] == "4.0"
