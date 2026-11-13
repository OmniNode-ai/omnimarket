# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host install orchestrator against the real reducer, file effect and readback compute (OMN-23957).

Every chain here is run through ``Plant``, which hands each published command to the node that owns the topic and each
published step to the real reducer fold. The reducer, not the test, decides the state each chain ends in.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_host_install_file_effect.models.model_host_install_file import (
    ModelHostInstallFileResult,
)
from omnimarket.nodes.node_host_install_orchestrator.handlers.handler_host_install_orchestrator import (
    TOPIC_FILE_REQUESTED,
    TOPIC_READBACK_REQUESTED,
    TOPIC_REGISTRY_REQUESTED,
    TOPIC_STEP,
    HandlerHostInstallOrchestrator,
)
from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text
from omnimarket.nodes.node_host_install_readback_compute.models.model_host_install_readback import (
    ModelHostInstallReadbackResult,
)
from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HostInstallTransitionError,
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsResult,
)
from tests.nodes.node_host_install_file_effect.support import FakeHostInstallFiles
from tests.nodes.node_host_install_orchestrator.support import (
    NEW_SERVICE,
    NEW_TIMER,
    OLD_SERVICE,
    OLD_TIMER,
    SERVICE_PATH,
    TIMER_PATH,
    Plant,
    StepClock,
    orchestrator_overlay,
    request,
)

pytestmark = pytest.mark.unit

S = EnumHostInstallState
RENDER = NEW_SERVICE + "\n" + NEW_TIMER
OLD = OLD_SERVICE + "\n" + OLD_TIMER


def build(
    *,
    render_ok: bool = True,
    install_ok: bool = True,
    install_timer: str = NEW_TIMER,
    run_ok: bool = True,
    drop_manifest_on_restore: bool = False,
) -> Plant:
    files = FakeHostInstallFiles()
    files.seed(SERVICE_PATH, OLD_SERVICE)
    files.seed(TIMER_PATH, OLD_TIMER)
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    return Plant(
        orchestrator,
        files,
        render_ok=render_ok,
        install_ok=install_ok,
        install_timer=install_timer,
        run_ok=run_ok,
        drop_manifest_on_restore=drop_manifest_on_restore,
    )


GOLDEN_TOPICS = [
    TOPIC_REGISTRY_REQUESTED,  # dry-run render
    TOPIC_STEP,  # request
    TOPIC_FILE_REQUESTED,  # snapshot
    TOPIC_STEP,  # snapshot ok
    TOPIC_STEP,  # install started
    TOPIC_REGISTRY_REQUESTED,  # install
    TOPIC_STEP,  # install ok
    TOPIC_FILE_REQUESTED,  # read
    TOPIC_READBACK_REQUESTED,
    TOPIC_STEP,  # verify ok
    TOPIC_REGISTRY_REQUESTED,  # run read
    TOPIC_STEP,  # run ok
    "onex.evt.omnimarket.host-install-completed.v1",
]


def test_golden_chain_installs_and_the_reducer_reads_run_ok() -> None:
    plant = build()
    plant.run(request())
    assert plant.topics == GOLDEN_TOPICS
    assert plant.refusals == []
    assert [r.state for r in plant.records] == [
        S.REQUESTED,
        S.SNAPSHOTTED,
        S.INSTALLING,
        S.INSTALLED,
        S.VERIFIED,
        S.RUN_OK,
    ]
    final = plant.records[-1]
    assert final.render_digest == digest_text(RENDER, "sha256")
    assert final.installed_digest == final.render_digest
    assert final.rollback_target_digest == digest_text(OLD, "sha256")
    assert plant.completed is not None
    assert plant.completed.outcome == "installed"
    assert plant.completed.final_state is S.RUN_OK
    assert plant.orchestrator.known_installs == ()


def test_each_step_carries_the_record_the_reducer_last_returned() -> None:
    plant = build()
    plant.run(request())
    priors = [step.prior.state if step.prior else None for step in plant.steps]
    assert priors == [
        None,
        S.REQUESTED,
        S.SNAPSHOTTED,
        S.INSTALLING,
        S.INSTALLED,
        S.VERIFIED,
    ]


def test_golden_rollback_chain_after_a_readback_that_differs() -> None:
    plant = build(install_timer="[Timer]\nOnActiveSec=77s\n")
    plant.run(request())
    states = [r.state for r in plant.records]
    assert S.VERIFIED not in states
    assert states[-2:] == [S.FAILED, S.ROLLED_BACK]
    assert plant.records[-1].installed_digest == digest_text(OLD, "sha256")
    assert plant.files.read_text(SERVICE_PATH) == OLD_SERVICE
    assert plant.files.read_text(TIMER_PATH) == OLD_TIMER
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"


def test_install_failure_restores_the_snapshot() -> None:
    plant = build(install_ok=False)
    plant.run(request())
    assert [r.state for r in plant.records][-2:] == [S.FAILED, S.ROLLED_BACK]
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"


def test_run_failure_restores_the_snapshot() -> None:
    plant = build(run_ok=False)
    plant.run(request())
    states = [r.state for r in plant.records]
    assert S.VERIFIED in states
    assert states[-2:] == [S.FAILED, S.ROLLED_BACK]
    assert plant.files.read_text(TIMER_PATH) == OLD_TIMER


def test_error_chain_render_failure_publishes_no_step() -> None:
    plant = build(render_ok=False)
    plant.run(request())
    assert plant.topics == [
        TOPIC_REGISTRY_REQUESTED,
        "onex.evt.omnimarket.host-install-completed.v1",
    ]
    assert plant.records == []
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert "uv is not on PATH" in plant.completed.detail


def test_error_chain_snapshot_failure_ends_failed_without_installing() -> None:
    files = FakeHostInstallFiles()
    files.seed(SERVICE_PATH, "x" * 2_000_000)
    plant = Plant(
        HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock()), files
    )
    plant.run(request())
    assert [r.state for r in plant.records] == [S.REQUESTED, S.FAILED]
    assert "install" not in [step.event.step.value for step in plant.steps]
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"
    assert "max_text_bytes" in plant.completed.detail


def test_error_chain_restore_failure_leaves_the_record_failed() -> None:
    plant = build(install_ok=False, drop_manifest_on_restore=True)
    plant.run(request())
    final = plant.records[-1]
    assert final.state is S.FAILED
    assert final.rollback_failures == 1
    assert plant.completed is not None
    assert plant.completed.outcome == "failed"


def test_results_for_an_unknown_install_or_phase_are_ignored() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    key = uuid4()
    registry = ModelRegistryUnitsResult(unit="u", mode="install", status="success")
    assert orchestrator.decide(registry, key) == ()
    readback = ModelHostInstallReadbackResult(
        correlation_id=key,
        host="host-a",
        unit="unit-a",
        status="match",
        render_digest="ab",
        installed_digest="ab",
        detail="",
    )
    assert orchestrator.decide(readback, key) == ()
    start = request(key)
    assert len(orchestrator.decide(start, key)) == 1
    assert (
        orchestrator.decide(readback, key) == ()
    )  # a readback while the render is awaited
    file_result = ModelHostInstallFileResult(
        correlation_id=key,
        host="host-a",
        unit="unit-a",
        mode="restore",
        status="success",
        present=True,
        text="",
        digest="ab",
        backup_dir="/b",
        detail="",
    )
    assert orchestrator.decide(file_result, key) == ()
    assert orchestrator.decide(object(), key) == ()


def test_working_memory_drops_the_oldest_install_past_the_limit() -> None:
    overlay = orchestrator_overlay().model_copy(update={"context_limit": 2})
    orchestrator = HandlerHostInstallOrchestrator(overlay, StepClock())
    keys = [uuid4() for _ in range(3)]
    for key in keys:
        orchestrator.decide(request(key), key)
    assert orchestrator.known_installs == (keys[1], keys[2])


def test_failing_control_a_second_step_with_a_stale_prior_is_refused() -> None:
    """The orchestrator waits for the record: the same two steps sent back to back (prior still absent) are refused."""
    plant = build()
    plant.run(request())
    request_step, snapshot_step = plant.steps[0], plant.steps[1]
    assert snapshot_step.prior is not None
    stale = ModelHostInstallStepEvent.model_validate(snapshot_step.event.model_dump())
    with pytest.raises(HostInstallTransitionError):
        fold(request_step.prior, stale, plant.reducer_overlay)
    assert (
        fold(snapshot_step.prior, stale, plant.reducer_overlay).state is S.SNAPSHOTTED
    )


def test_handle_wraps_decide_in_envelopes_named_by_topic() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    start = request()
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=start,
        correlation_id=start.correlation_id,
    )
    output = asyncio.run(orchestrator.handle(envelope))
    assert [e.event_type for e in output.events] == [TOPIC_REGISTRY_REQUESTED]
    assert output.events[0].correlation_id == start.correlation_id
