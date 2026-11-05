# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay selection, request and spec shapes, and the two existing spi protocols over the node."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_spi.protocols.workflow_orchestration.protocol_workflow_orchestrator import (
    ProtocolWorkflowOrchestrator,
)
from omnibase_spi.protocols.workflow_orchestration.protocol_workflow_persistence import (
    ProtocolLiteralWorkflowStateStore,
)
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_run_engine_effect import NodeRunEngineEffect
from omnimarket.nodes.node_run_engine_effect.backends.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_run_engine_effect.backends.backend_temporal_self_hosted import (
    BackendTemporalSelfHosted,
)
from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    ProtocolRunEngineBackend,
    RunEngineUnsupportedError,
)
from omnimarket.nodes.node_run_engine_effect.handlers.adapter_spi_orchestrator import (
    AdapterSpiOrchestrator,
    instance_id_for,
)
from omnimarket.nodes.node_run_engine_effect.handlers.adapter_spi_state_store import (
    AdapterSpiStateStore,
)
from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)
from omnimarket.nodes.node_run_engine_effect.handlers.overlay_run_engine import (
    NODE_OVERLAY_ID,
    build_backend,
    load_run_engine_overlay,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumRunEngineBackend,
    EnumRunEngineOperation,
    EnumRunStatus,
    EnumRunStepKind,
    ModelRunEngineRequest,
    ModelRunSpec,
    ModelRunStepSpec,
)

pytestmark = pytest.mark.unit

TEMPORAL_DOC: dict[str, Any] = {
    "run_engine_backend": "temporal_self_hosted",
    "temporal_self_hosted": {
        "target_host": "overlay-supplied:1",
        "namespace": "ns",
        "task_queue": "tq",
        "command_start_to_close_seconds": 60,
        "heartbeat_timeout_seconds": 5,
        "heartbeat_interval_seconds": 1.0,
        "retry_initial_seconds": 1,
        "retry_max_seconds": 2,
        "retry_max_attempts": 2,
        "connect_timeout_seconds": 3,
    },
}


def write_overlay(root: Path, document: object) -> None:
    (root / NODE_OVERLAY_ID).mkdir(parents=True)
    (root / NODE_OVERLAY_ID / "overlay.yaml").write_text(yaml.safe_dump(document))


def test_no_overlay_selects_the_in_memory_backend(tmp_path: Path) -> None:
    overlay = load_run_engine_overlay(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert overlay.run_engine_backend is EnumRunEngineBackend.IN_MEMORY
    assert isinstance(build_backend(overlay), BackendInMemory)
    assert isinstance(NodeRunEngineEffect(environ={}).backend, BackendInMemory)


def test_the_overlay_selects_the_temporal_backend_with_its_settings(
    tmp_path: Path,
) -> None:
    write_overlay(tmp_path, TEMPORAL_DOC)
    node = NodeRunEngineEffect(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert isinstance(node.backend, BackendTemporalSelfHosted)
    assert node.backend.durable
    assert node.backend.name == "temporal_self_hosted"


@pytest.mark.parametrize(
    "document",
    [
        {"run_engine_backend": "temporal_self_hosted"},
        {
            "run_engine_backend": "in_memory",
            "temporal_self_hosted": TEMPORAL_DOC["temporal_self_hosted"],
        },
        {"run_engine_backend": "archon"},
        {"run_engine_backend": "in_memory", "surprise": 1},
        {
            "run_engine_backend": "temporal_self_hosted",
            "temporal_self_hosted": {"target_host": "x"},
        },
    ],
)
def test_a_broken_overlay_refuses_and_is_never_read_as_absent(
    tmp_path: Path, document: object
) -> None:
    write_overlay(tmp_path, document)
    with pytest.raises(NodeOverlayError):
        load_run_engine_overlay(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_both_backends_satisfy_the_backend_protocol(tmp_path: Path) -> None:
    write_overlay(tmp_path, TEMPORAL_DOC)
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    assert isinstance(NodeRunEngineEffect(environ={}).backend, ProtocolRunEngineBackend)
    assert isinstance(
        NodeRunEngineEffect(environ=env).backend, ProtocolRunEngineBackend
    )


def test_step_and_spec_shapes_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ModelRunStepSpec(name="a", kind=EnumRunStepKind.COMMAND)
    with pytest.raises(ValidationError):
        ModelRunStepSpec(name="a", kind=EnumRunStepKind.WAIT_RELEASE)
    with pytest.raises(ValidationError):
        ModelRunStepSpec(
            name="a", kind=EnumRunStepKind.COMMAND, argv=("x",), condition="c"
        )
    step = ModelRunStepSpec(name="a", kind=EnumRunStepKind.COMMAND, argv=("x",))
    with pytest.raises(ValidationError):
        ModelRunSpec(run_key="k", steps=(step, step))
    with pytest.raises(ValidationError):
        ModelRunSpec(run_key="k", steps=())
    with pytest.raises(ValidationError):
        ModelRunEngineRequest(operation=EnumRunEngineOperation.RELEASE_RUN, run_key="k")


def test_the_existing_spi_protocols_are_satisfied_and_answer_over_the_engine() -> None:
    async def scenario() -> None:
        backend = NodeRunEngineEffect(environ={}).backend
        handler = HandlerRunEngine(backend)
        orchestrator = AdapterSpiOrchestrator(handler)
        store = AdapterSpiStateStore(backend)
        assert isinstance(orchestrator, ProtocolWorkflowOrchestrator)
        assert isinstance(store, ProtocolLiteralWorkflowStateStore)

        key = "spi-run-1"
        spec = ModelRunSpec(
            run_key=key,
            steps=(
                ModelRunStepSpec(
                    name="hold", kind=EnumRunStepKind.WAIT_RELEASE, condition="gate"
                ),
            ),
        )
        started = await orchestrator.orchestrate_operation(
            "start_run",
            key,
            uuid4(),
            type("P", (), {"parameters": {"run": spec.model_dump_json()}})(),
        )
        assert started.success
        assert started.error_code is None
        again = await orchestrator.orchestrate_operation(
            "start_run",
            key,
            uuid4(),
            type("P", (), {"parameters": {"run": spec.model_dump_json()}})(),
        )
        assert not again.success
        assert again.error_code == "run_engine.duplicate"

        await backend.settle(key, 5)
        execution = await orchestrator.get_execution_state(key)
        assert execution is not None
        assert execution.state == "paused"
        assert execution.total_steps == 1
        assert execution.instance_id == instance_id_for(key)
        assert await orchestrator.get_execution_state("spi-nobody") is None
        health = await orchestrator.health_check()
        assert health.is_healthy()
        assert health.node_type == "EFFECT"

        instance_id = store.register_run_key(key)
        snapshot = await store.load_workflow_instance("run_engine", instance_id)
        assert snapshot is not None
        assert snapshot.state == "paused"
        listed = await store.query_workflow_instances(
            "run_engine", "paused", None, 10, 0
        )
        assert [s.instance_id for s in listed] == [instance_id]
        assert await store.load_workflow_instance("other_type", instance_id) is None

        assert await store.save_workflow_instance(snapshot)
        assert await store.lock_workflow_instance("run_engine", instance_id, "a", 5)
        assert not await store.lock_workflow_instance("run_engine", instance_id, "b", 5)
        assert await store.unlock_workflow_instance("run_engine", instance_id, "a")
        assert await store.delete_workflow_instance("run_engine", instance_id)

        await backend.cancel_run(key)

    asyncio.run(scenario())


def test_a_durable_backend_refuses_the_caller_owned_store_operations(
    tmp_path: Path,
) -> None:
    write_overlay(tmp_path, TEMPORAL_DOC)
    backend = NodeRunEngineEffect(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    ).backend
    store = AdapterSpiStateStore(backend)

    async def scenario() -> None:
        for call in (
            store.delete_workflow_instance("run_engine", uuid4()),
            store.lock_workflow_instance("run_engine", uuid4(), "o", 1),
            store.unlock_workflow_instance("run_engine", uuid4(), "o"),
        ):
            with pytest.raises(RunEngineUnsupportedError):
                await call

    asyncio.run(scenario())
    assert EnumRunStatus.NOT_FOUND.value == "not_found"
