# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored row is the orchestrator's whole memory: a recreated runtime resumes from it (OMN-24058).

Each restart test rebuilds the handler mid-flow — a new handler over the same row store, a fresh clock, no other
memory — and the flow still ends where the unbroken flow ends. The control is the same rebuild over an empty store:
there the job result is ignored and the deploy never finishes, the red state the stored row fixes.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.state_io.state_store_adapter import (
    CONTEXTVAR_STATE_IO_ROWS,
)

from omnimarket.models.host_install_state import ModelHostInstallReduceCommand
from omnimarket.nodes.node_branch_deploy_orchestrator.handlers.handler_branch_deploy_orchestrator import (
    HandlerBranchDeployOrchestrator,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.models import (
    EnumBranchDeployNext,
    EnumBranchDeployPhase,
    ModelBranchDeployContext,
    ModelDeployJobCompleted,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.orchestration.row_store import (
    InMemoryBranchDeployRowStore,
    StateIoBranchDeployRowStore,
    decode_row,
    encode_row,
    read_active_state_io_rows,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.state_codec import StateIoCodec
from tests.nodes.node_branch_deploy_orchestrator.support import (
    FLOWS,
    Plant,
    StepClock,
    job_event,
    orchestrator_overlay,
    request,
)

pytestmark = pytest.mark.unit

PHASE = EnumBranchDeployPhase
#: The overlay example's ``context_limit``, the bound of a configured in-memory row store.
LIMIT = orchestrator_overlay().context_limit


def test_restart_mid_deploy_resumes_from_the_stored_row() -> None:
    shared = InMemoryBranchDeployRowStore(LIMIT)
    req = request()
    plant = Plant(
        **FLOWS["golden"], store=shared, restart_before=ModelDeployJobCompleted
    )
    plant.run(req)
    assert plant.completed is not None
    assert plant.completed.outcome == "deployed"
    row, _version = shared.load(str(req.correlation_id))
    assert row is not None
    assert row.phase is PHASE.DONE


def test_restart_mid_rollback_resumes_from_the_stored_row() -> None:
    shared = InMemoryBranchDeployRowStore(LIMIT)
    req = request()
    plant = Plant(
        **FLOWS["wrong_build"],
        store=shared,
        restart_before=ModelDeployJobCompleted,
        restart_nth=2,
    )
    plant.run(req)
    assert plant.completed is not None
    assert plant.completed.outcome == "rolled_back"
    row, _version = shared.load(str(req.correlation_id))
    assert row is not None
    assert row.phase is PHASE.DONE


def test_failing_control_without_the_stored_row_the_deploy_never_finishes() -> None:
    """The same rebuild over an empty store: the job result is ignored and nothing more is published."""
    plant = Plant(
        **FLOWS["golden"],
        restart_before=ModelDeployJobCompleted,
        lose_store_on_restart=True,
    )
    plant.run(request())
    assert plant.completed is None
    assert plant.restart_log_index is not None
    assert plant.log[plant.restart_log_index :] == []


def test_a_message_for_an_unknown_deploy_is_ignored_and_stores_nothing() -> None:
    store = InMemoryBranchDeployRowStore(LIMIT)
    orchestrator = HandlerBranchDeployOrchestrator(
        orchestrator_overlay(), StepClock(), store=store
    )
    key = uuid4()
    assert orchestrator.decide(job_event("completed_success", key), key) == ()
    assert store.load(str(key)) == (None, 0)


def test_a_finished_deploy_ignores_a_late_result() -> None:
    shared = InMemoryBranchDeployRowStore(LIMIT)
    req = request()
    plant = Plant(**FLOWS["golden"], store=shared)
    plant.run(req)
    row, version = shared.load(str(req.correlation_id))
    assert row is not None
    assert row.phase is PHASE.DONE
    late = HandlerBranchDeployOrchestrator(
        orchestrator_overlay(), StepClock(), store=shared
    )
    key = req.correlation_id
    assert late.decide(job_event("completed_success", key), key) == ()
    assert shared.load(str(key)) == (row, version)


def test_row_encoding_is_the_context_plus_the_three_runtime_keys() -> None:
    context = ModelBranchDeployContext(request=request(), phase=PHASE.JOB)
    payload = json.loads(encode_row(context))
    assert payload["state"] == "job"
    assert payload["in_flight"] is False
    assert payload["tenant_id"] == "platform"
    done = context.model_copy(update={"phase": PHASE.DONE})
    assert json.loads(encode_row(done))["state"] == "COMPLETED"
    assert decode_row(encode_row(context)) == context
    abandoned = json.loads(encode_row(context))
    abandoned["failure_reason"] = "recover_stale_rows gave the row up"
    assert decode_row(json.dumps(abandoned)) == context


def test_in_memory_store_compare_and_set_refuses_a_stale_version() -> None:
    store = InMemoryBranchDeployRowStore(2)
    active = ModelBranchDeployContext(request=request(), phase=PHASE.JOB)
    key = str(active.request.correlation_id)
    assert store.compare_and_set(key, 0, active) is True
    assert store.compare_and_set(key, 0, active) is False
    probing = active.model_copy(update={"phase": PHASE.PROBE})
    assert store.compare_and_set(key, 1, probing) is True
    assert store.load(key) == (probing, 2)
    finished = ModelBranchDeployContext(request=request(), phase=PHASE.DONE)
    later = ModelBranchDeployContext(request=request(), phase=PHASE.DONE)
    assert store.compare_and_set(str(finished.request.correlation_id), 0, finished)
    assert store.compare_and_set(str(later.request.correlation_id), 0, later)
    assert store.load(str(finished.request.correlation_id)) == (None, 0)
    assert store.load(key) == (probing, 2)
    assert store.load(str(later.request.correlation_id)) == (later, 1)


def test_state_io_path_reads_the_runtime_row_and_stages_the_next() -> None:
    handler = HandlerBranchDeployOrchestrator(orchestrator_overlay(), StepClock())
    req = request()
    key = req.correlation_id
    context = ModelBranchDeployContext(request=req, phase=PHASE.JOB)
    token = CONTEXTVAR_STATE_IO_ROWS.set({str(key): (encode_row(context), 3)})
    try:
        output = asyncio.run(handler.handle_async(job_event("completed_success", key)))
        flushed = StateIoCodec().flush(str(key))
    finally:
        CONTEXTVAR_STATE_IO_ROWS.reset(token)
    assert len(output.events) == 1
    event = output.events[0]
    assert isinstance(event, ModelHostInstallReduceCommand)
    assert not isinstance(event, ModelEventEnvelope)
    assert event.prior is None
    assert event.event.correlation_id == key
    assert flushed is not None
    assert decode_row(flushed) == context.model_copy(
        update={
            "phase": PHASE.RECORDING,
            "next_action": EnumBranchDeployNext.SEND_PROBE,
        }
    )
    assert StateIoCodec().flush(str(key)) is None


def test_state_io_store_refuses_when_rows_are_not_bound() -> None:
    assert read_active_state_io_rows() is None
    with pytest.raises(RuntimeError, match="state_io rows are not bound"):
        StateIoBranchDeployRowStore().load("x")
