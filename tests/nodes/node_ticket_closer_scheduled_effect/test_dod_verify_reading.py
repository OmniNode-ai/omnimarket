# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The DoD verifier as the ticket closer's verifier: one state read as one closer reading (OMN-24138)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.model_dod_verify_start_command import ModelDodVerifyStartCommand
from omnimarket.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.dod_verify_reading import (
    DodVerifyReader,
    reading_from_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def check(
    evidence_id: str,
    status: EnumEvidenceCheckStatus,
    binds: tuple[str, ...] = (),
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description=f"check {evidence_id}",
        status=status,
        binds_ac=binds,
    )


def state(
    checks: list[ModelEvidenceCheckResult], **over: object
) -> ModelDodVerifyState:
    fields: dict[str, object] = {
        "correlation_id": uuid4(),
        "ticket_id": "OMN-1",
        "started_at": NOW,
        "completed_at": NOW,
        "checks": checks,
        "total_checks": len(checks),
    }
    fields.update(over)
    return ModelDodVerifyState.model_validate(fields)


def verified_state() -> ModelDodVerifyState:
    return state(
        [
            check("dod-1", EnumEvidenceCheckStatus.VERIFIED, ("AC1",)),
            check("dod-2", EnumEvidenceCheckStatus.VERIFIED, ("AC2",)),
        ],
        status=EnumDodVerifyStatus.VERIFIED,
        verified_count=2,
        behavior_proving_count=1,
        readback_proving_count=1,
        contract_commit_sha="abc123",
    )


def test_a_verified_state_reads_as_a_run_with_every_criterion_bound() -> None:
    reading = reading_from_state("OMN-1", verified_state())
    assert reading.ran is True
    assert reading.error_code == ""
    assert reading.bound_criteria == ("AC1", "AC2")
    assert reading.total_checks == 2
    assert reading.verified_count == 2
    assert reading.behavior_proving_count == 1
    assert reading.head_revision == "abc123"
    assert reading.exercises_running_system is True


def test_a_state_without_a_readback_does_not_exercise_the_running_system() -> None:
    plain = verified_state().model_copy(update={"readback_proving_count": 0})
    assert reading_from_state("OMN-1", plain).exercises_running_system is False


def test_a_failed_check_leaves_its_criterion_unbound() -> None:
    failed = state(
        [
            check("dod-1", EnumEvidenceCheckStatus.VERIFIED, ("AC1",)),
            check("dod-2", EnumEvidenceCheckStatus.FAILED, ("AC2",)),
        ],
        status=EnumDodVerifyStatus.FAILED,
        verified_count=1,
        failed_count=1,
        error_message="EVIDENCE_CHECK_FAILED: dod-2",
    )
    reading = reading_from_state("OMN-1", failed)
    assert reading.ran is True
    assert reading.bound_criteria == ("AC1",)
    assert reading.error_code == "EVIDENCE_CHECK_FAILED"
    assert reading.failed_count == 1


def test_control_an_unresolved_contract_judged_nothing() -> None:
    unresolved = state(
        [check("contract", EnumEvidenceCheckStatus.FAILED)],
        status=EnumDodVerifyStatus.FAILED,
        failed_count=1,
        error_message="EVIDENCE_CHECK_FAILED: contract: REPO_CONTRACT_UNRESOLVED: gateway merged_pr: no answer",
    )
    reading = reading_from_state("OMN-1", unresolved)
    assert reading.ran is False
    assert reading.error_code == "EVIDENCE_CHECK_FAILED"
    assert "REPO_CONTRACT_UNRESOLVED" in reading.error_detail
    assert reading.bound_criteria == ()


def test_control_a_state_with_no_checks_judged_nothing() -> None:
    reading = reading_from_state("OMN-1", state([]))
    assert reading.ran is False
    assert reading.error_code == "VERIFY_COULD_NOT_RUN"


class StubHandler(HandlerDodVerify):
    """A verifier handler that returns what it was given and records the commands it ran."""

    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        super().__init__()
        self._result = result
        self._raises = raises
        self.commands: list[ModelDodVerifyStartCommand] = []

    def handle(
        self,
        payload: ModelDodVerifyStartCommand | dict[str, object],
        *,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState | dict[str, object]:
        assert isinstance(payload, ModelDodVerifyStartCommand)
        self.commands.append(payload)
        if self._raises is not None:
            raise self._raises
        assert isinstance(self._result, (ModelDodVerifyState, dict))
        return self._result


def test_the_reader_runs_the_verifier_for_the_ticket_and_audience() -> None:
    stub = StubHandler(verified_state())
    reading = DodVerifyReader(stub).verify("OMN-1", "hosted")
    assert reading.ran is True
    command = stub.commands[0]
    assert command.ticket_id == "OMN-1"
    assert command.execution_audience is not None
    assert command.execution_audience.value == "hosted"


def test_error_a_raising_verifier_reads_as_did_not_run() -> None:
    reading = DodVerifyReader(StubHandler(raises=RuntimeError("x"))).verify(
        "OMN-1", "hosted"
    )
    assert reading.ran is False
    assert reading.error_code == "VERIFY_RAISED"
    assert reading.error_detail == "RuntimeError"


def test_error_a_verifier_that_returns_no_state_is_refused_by_name() -> None:
    with pytest.raises(TypeError, match="dict"):
        DodVerifyReader(StubHandler({"not": "a state"})).verify("OMN-1", "hosted")
