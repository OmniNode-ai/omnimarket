"""Golden-chain tests for node_dispatch_worker_execution_effect."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
    _DELEGATION_EVENT_TYPE,
    _TOPIC_DELEGATION_REQUEST,
    HandlerDispatchWorkerExecution,
)
from omnimarket.nodes.node_dispatch_worker_execution_effect.models import (
    EnumDispatchWorkerExecutionStatus,
    ModelCompiledDispatchWorker,
    ModelDispatchWorkerExecutionInput,
    ModelDispatchWorkerSpecArtifact,
)


def _compiled(*, rejected_reason: str = "") -> ModelCompiledDispatchWorker:
    return ModelCompiledDispatchWorker(
        validated_task_description="[fixer] session-disp-001-omn-9874: Run ticket",
        validated_prompt_template="Run the ticket pipeline for OMN-9874.",
        proposed_agent_spawn_args={
            "name": "session-disp-001-omn-9874",
            "team_name": "Omninode",
            "model": "sonnet",
            "subagent_type": "general-purpose",
        },
        collision_fence_embeds=("omnimarket#415",),
        rejected_reason=rejected_reason,
    )


def _artifact(*, rejected_reason: str = "") -> ModelDispatchWorkerSpecArtifact:
    return ModelDispatchWorkerSpecArtifact(
        session_id="sess-test",
        ticket_id="OMN-9874",
        dispatch_id="disp-001",
        correlation_chain="sess-test.disp-001.OMN-9874",
        compiled_at=datetime.now(tz=UTC),
        dispatch_worker=_compiled(rejected_reason=rejected_reason),
    )


@pytest.mark.unit
def test_executes_loaded_spec_and_writes_receipt(tmp_path: Path) -> None:
    handler = HandlerDispatchWorkerExecution()
    cid = uuid4()
    result = handler.handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=cid,
            artifacts=(_artifact(),),
            receipt_dir=str(tmp_path / "receipts"),
        )
    )

    assert result.total_delegated == 1
    assert result.total_failed == 0
    assert len(result.delegation_payloads) == 1
    payload = result.delegation_payloads[0]
    assert payload.event_type == _DELEGATION_EVENT_TYPE
    assert payload.topic == _TOPIC_DELEGATION_REQUEST
    assert payload.payload["task_type"] == "agent_dispatch"
    assert payload.payload["ticket_id"] == "OMN-9874"
    assert payload.payload["prompt"] == "Run the ticket pipeline for OMN-9874."

    outcome = result.outcomes[0]
    assert outcome.status == EnumDispatchWorkerExecutionStatus.DELEGATED
    assert outcome.delegated is True
    receipt_path = Path(outcome.receipt_path)
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "delegated"
    assert receipt["delegation_topic"] == _TOPIC_DELEGATION_REQUEST


@pytest.mark.unit
def test_loads_persisted_spec_artifact(tmp_path: Path) -> None:
    artifact_path = tmp_path / "disp-001-omn-9874.json"
    artifact_path.write_text(_artifact().model_dump_json())

    result = HandlerDispatchWorkerExecution().handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=uuid4(),
            artifact_paths=(str(artifact_path),),
            receipt_dir=str(tmp_path / "receipts"),
        )
    )

    assert result.total_delegated == 1
    assert result.outcomes[0].ticket_id == "OMN-9874"


@pytest.mark.unit
def test_retry_skips_existing_receipt(tmp_path: Path) -> None:
    handler = HandlerDispatchWorkerExecution()
    command = ModelDispatchWorkerExecutionInput(
        correlation_id=uuid4(),
        artifacts=(_artifact(),),
        receipt_dir=str(tmp_path / "receipts"),
    )

    first = handler.handle(command)
    second = handler.handle(command)

    assert first.total_delegated == 1
    assert second.total_delegated == 0
    assert second.total_skipped == 1
    assert (
        second.outcomes[0].status == EnumDispatchWorkerExecutionStatus.SKIPPED_DUPLICATE
    )
    assert second.delegation_payloads == ()


@pytest.mark.unit
def test_existing_receipt_skips_before_payload_emission(tmp_path: Path) -> None:
    handler = HandlerDispatchWorkerExecution()
    artifact = _artifact()
    command = ModelDispatchWorkerExecutionInput(
        correlation_id=uuid4(),
        artifacts=(artifact,),
        receipt_dir=str(tmp_path / "receipts"),
    )
    receipt_path = handler._receipt_path(Path(command.receipt_dir), artifact)
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text("{}\n")

    result = handler.handle(command)

    assert result.total_delegated == 0
    assert result.total_skipped == 1
    assert (
        result.outcomes[0].status == EnumDispatchWorkerExecutionStatus.SKIPPED_DUPLICATE
    )
    assert result.delegation_payloads == ()


@pytest.mark.unit
def test_rejected_compiled_spec_emits_rejected_outcome(tmp_path: Path) -> None:
    result = HandlerDispatchWorkerExecution().handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=uuid4(),
            artifacts=(_artifact(rejected_reason="worker already active"),),
            receipt_dir=str(tmp_path / "receipts"),
        )
    )

    assert result.total_rejected == 1
    assert result.total_delegated == 0
    assert result.outcomes[0].status == EnumDispatchWorkerExecutionStatus.REJECTED
    assert "worker already active" in result.outcomes[0].error
    assert not (tmp_path / "receipts").exists()


@pytest.mark.unit
def test_dry_run_emits_no_payload_or_receipt(tmp_path: Path) -> None:
    result = HandlerDispatchWorkerExecution().handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=uuid4(),
            artifacts=(_artifact(),),
            receipt_dir=str(tmp_path / "receipts"),
            dry_run=True,
        )
    )

    assert result.total_skipped == 1
    assert result.total_delegated == 0
    assert result.delegation_payloads == ()
    assert result.outcomes[0].status == EnumDispatchWorkerExecutionStatus.DRY_RUN
    assert not (tmp_path / "receipts").exists()


@pytest.mark.unit
def test_duplicate_specs_rejected(tmp_path: Path) -> None:
    artifact = _artifact()
    with pytest.raises(ValueError, match="Duplicate dispatch-worker spec"):
        HandlerDispatchWorkerExecution().handle(
            ModelDispatchWorkerExecutionInput(
                correlation_id=uuid4(),
                artifacts=(artifact, artifact),
                receipt_dir=str(tmp_path / "receipts"),
            )
        )


@pytest.mark.unit
async def test_delegation_payload_is_publishable(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    result = HandlerDispatchWorkerExecution().handle(
        ModelDispatchWorkerExecutionInput(
            correlation_id=uuid4(),
            artifacts=(_artifact(),),
            receipt_dir=str(tmp_path / "receipts"),
        )
    )

    await event_bus.start()
    try:
        for payload in result.delegation_payloads:
            await event_bus.publish(
                payload.topic,
                key=None,
                value=json.dumps(payload.payload).encode(),
            )

        history = await event_bus.get_event_history(topic=_TOPIC_DELEGATION_REQUEST)
        assert len(history) == 1
        deserialized = json.loads(history[0].value)
        assert deserialized["task_type"] == "agent_dispatch"
        assert deserialized["correlation_chain"] == "sess-test.disp-001.OMN-9874"
    finally:
        await event_bus.close()
