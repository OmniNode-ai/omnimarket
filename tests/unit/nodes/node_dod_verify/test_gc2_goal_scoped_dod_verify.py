"""Goal-scoped DoD verification (OMN-20025 / GC.2)."""

from __future__ import annotations

from uuid import UUID

import pytest
from omnibase_core.enums.governance.enum_dod_eval_verification_status import (
    EnumDodEvalVerificationStatus,
)
from omnibase_core.enums.ticket.enum_dod_check_type import EnumDodCheckType
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_evidence_check import (
    ModelDodEvidenceCheck,
)
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.governance.model_dod_eval_input import ModelDodEvalInput
from omnibase_core.models.governance.model_dod_eval_outcome_reducer import (
    resolve_dod_eval_outcome as resolve_core_dod_eval_outcome,
)
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)
from pydantic import ValidationError

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    resolve_dod_eval_outcome,
)
from omnimarket.nodes.node_projection_dod_verdict.models import EnumDodEvalRefusal

GOAL_ID = UUID("747a3ba5-2ae9-4fa3-8c98-4d7475ce30c5")
PARENT_GOAL_ID = UUID("8bdb32d3-11c0-4748-bdde-76a9ec73c673")
CONTRACT_REVISION = UUID("00000000-0000-4000-8000-000000000001")
GOAL_SCHEMA_VERSION = "1.0.0"


def _disposition_item() -> ModelContractDodItem:
    return ModelContractDodItem(
        id="goal-disposition",
        description="Calling lane records its disposition",
        checks=[
            ModelDodEvidenceCheck(
                check_type=EnumDodCheckType.DISPOSITION,
                check_value="lane-verdict",
            )
        ],
    )


@pytest.mark.unit
def test_goal_start_command_accepts_inline_contract_without_contract_path() -> None:
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-20025",
        goal_id=GOAL_ID,
        parent_goal_id=PARENT_GOAL_ID,
        level="workflow_lane",
        contract_revision=CONTRACT_REVISION,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        dod_evidence=(_disposition_item(),),
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert command.contract_path is None
    assert command.goal_id == GOAL_ID
    assert command.parent_goal_id == PARENT_GOAL_ID
    assert command.contract_revision == CONTRACT_REVISION
    assert command.dod_evidence[0].checks[0].check_type is EnumDodCheckType.DISPOSITION


@pytest.mark.unit
def test_goal_start_command_refuses_no_contract_or_empty_inline_contract() -> None:
    with pytest.raises(ValidationError):
        ModelDodVerifyStartCommand(
            ticket_id="OMN-20025",
            goal_id=GOAL_ID,
            contract_revision=CONTRACT_REVISION,
            contract_schema_version=GOAL_SCHEMA_VERSION,
            dod_evidence=(),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "schema_version",
    [None, "1.0.0-rc.1"],
    ids=("missing-version", "invalid-version"),
)
def test_goal_start_command_requires_valid_contract_schema_version(
    schema_version: str | None,
) -> None:
    payload: dict[str, object] = {
        "ticket_id": "OMN-20025",
        "goal_id": GOAL_ID,
        "level": "workflow_lane",
        "contract_revision": CONTRACT_REVISION,
        "dod_evidence": (_disposition_item(),),
        "execution_audience": EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    }
    if schema_version is not None:
        payload["contract_schema_version"] = schema_version

    with pytest.raises(
        ValidationError, match=r"contract_schema_version|schema_version"
    ):
        ModelDodVerifyStartCommand.model_validate(payload)


@pytest.mark.unit
def test_goal_disposition_only_refused_without_becoming_an_executable_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the disposition is carried through, never run or counted as proof."""
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-20025",
        goal_id=GOAL_ID,
        level="workflow_lane",
        contract_revision=CONTRACT_REVISION,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        dod_evidence=(_disposition_item(),),
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )
    handler = HandlerDodVerify()

    class Collector:
        occ_governance_ref = None
        occ_refresh_outcome = None
        occ_resolved_sha = None
        occ_ref_failure_code = None
        occ_ref_failure_cause = None
        lookup_failure_cause = None
        lookup_failure_code = None

        def collect_inline(
            self, ticket_id: str, dod_evidence: object, **_: object
        ) -> list[ModelEvidenceCheckResult]:
            assert ticket_id == "OMN-20025"
            assert len(dod_evidence) == 1  # type: ignore[arg-type]
            return [
                ModelEvidenceCheckResult(
                    evidence_id="goal-disposition",
                    description="stored disposition",
                    status=EnumEvidenceCheckStatus.VERIFIED,
                    is_disposition=True,
                )
            ]

    monkeypatch.setattr(handler, "_make_collector", lambda: Collector())
    state, completed = handler.run_verification(command)

    assert state.status.value == "verified"
    assert state.total_checks == 0
    assert state.verified_count == 0
    assert state.failed_count == 0
    assert state.behavior_proving_count == 0
    assert state.readback_proving_count == 0
    assert completed.goal_id == GOAL_ID
    assert completed.contract_revision == CONTRACT_REVISION
    verdict = resolve_dod_eval_outcome(
        status=completed.status,
        failed_count=completed.failed_count,
        total_checks=completed.total_checks,
        behavior_proving_count=completed.behavior_proving_count,
    )
    assert verdict.refusal is EnumDodEvalRefusal.NO_CHECKS_RUN


def _contract_entry_sha256(
    item: ModelContractDodItem, contract_schema_version: str
) -> str:
    return compute_contract_entry_sha256(
        {
            "ticket_id": "OMN-20025",
            "schema_version": contract_schema_version,
            "dod_evidence": [item.model_dump(mode="json")],
        },
        item.id,
    )


def _disposition_receipt(
    goal_id: UUID,
    *,
    item: ModelContractDodItem | None = None,
    contract_entry_sha256: str | None = None,
    with_entry_hash: bool = True,
    contract_schema_version: str = GOAL_SCHEMA_VERSION,
) -> dict[str, object]:
    contract_item = item or _disposition_item()
    return ModelDodReceipt(
        schema_version="1.0.0",
        ticket_id="OMN-20025",
        goal_id=str(goal_id),
        evidence_item_id=contract_item.id,
        check_type=EnumDodCheckType.DISPOSITION.value,
        check_value="lane-verdict",
        contract_entry_sha256=(
            contract_entry_sha256
            if not with_entry_hash
            else contract_entry_sha256
            or _contract_entry_sha256(contract_item, contract_schema_version)
        ),
        status=EnumReceiptStatus.PASS,
        run_timestamp="2026-09-29T12:00:00Z",
        commit_sha="a" * 40,
        runner="lane-runner",
        verifier="independent-reviewer",
        probe_command="read stored lane disposition",
        probe_stdout="PASS",
    ).model_dump(mode="json")


@pytest.mark.unit
def test_disposition_receipt_is_read_without_executing_its_check_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        collector,
        "_load_item_receipts",
        lambda *_: [_disposition_receipt(GOAL_ID)],
    )

    def forbidden_execution(*_: object, **__: object) -> None:
        pytest.fail("a disposition check_value must never execute")

    monkeypatch.setattr(collector, "_check_evidence_item", forbidden_execution)
    result = collector.collect_inline(
        "OMN-20025",
        (_disposition_item(),),
        goal_id=GOAL_ID,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert len(result) == 1
    assert result[0].is_disposition is True
    assert result[0].status is EnumEvidenceCheckStatus.VERIFIED


@pytest.mark.unit
def test_disposition_receipt_for_another_goal_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        collector,
        "_load_item_receipts",
        lambda *_: [_disposition_receipt(PARENT_GOAL_ID)],
    )
    result = collector.collect_inline(
        "OMN-20025",
        (_disposition_item(),),
        goal_id=GOAL_ID,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert len(result) == 1
    assert result[0].is_disposition is True
    assert result[0].status is EnumEvidenceCheckStatus.SKIPPED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry_hash", "with_entry_hash"),
    [(None, False), ("sha256:" + ("0" * 64), True)],
    ids=("missing-binding", "mismatched-binding"),
)
def test_disposition_receipt_requires_matching_item_hash(
    monkeypatch: pytest.MonkeyPatch,
    entry_hash: str | None,
    with_entry_hash: bool,
) -> None:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        collector,
        "_load_item_receipts",
        lambda *_: [
            _disposition_receipt(
                GOAL_ID,
                contract_entry_sha256=entry_hash,
                with_entry_hash=with_entry_hash,
            )
        ],
    )
    result = collector.collect_inline(
        "OMN-20025",
        (_disposition_item(),),
        goal_id=GOAL_ID,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert result[0].status is EnumEvidenceCheckStatus.FAILED
    assert "contract entry hash" in (result[0].message or "")


@pytest.mark.unit
def test_disposition_receipt_from_changed_item_with_same_id_and_check_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_item = ModelContractDodItem(
        id="goal-disposition",
        description="Old description with unchanged check.",
        checks=[
            ModelDodEvidenceCheck(
                check_type=EnumDodCheckType.DISPOSITION,
                check_value="lane-verdict",
            )
        ],
    )
    collector = EvidenceCollector()
    monkeypatch.setattr(
        collector,
        "_load_item_receipts",
        lambda *_: [
            _disposition_receipt(
                GOAL_ID,
                item=prior_item,
                contract_schema_version=GOAL_SCHEMA_VERSION,
            )
        ],
    )
    result = collector.collect_inline(
        "OMN-20025",
        (_disposition_item(),),
        goal_id=GOAL_ID,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert result[0].status is EnumEvidenceCheckStatus.FAILED
    assert "contract entry hash" in (result[0].message or "")


@pytest.mark.unit
def test_disposition_receipt_from_another_schema_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        collector,
        "_load_item_receipts",
        lambda *_: [_disposition_receipt(GOAL_ID)],
    )
    result = collector.collect_inline(
        "OMN-20025",
        (_disposition_item(),),
        goal_id=GOAL_ID,
        contract_schema_version="2.0.0",
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )

    assert result[0].status is EnumEvidenceCheckStatus.FAILED
    assert "contract entry hash" in (result[0].message or "")


@pytest.mark.unit
def test_goal_readback_only_refused_is_not_done() -> None:
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-20025",
        goal_id=GOAL_ID,
        level="workflow_lane",
        contract_revision=CONTRACT_REVISION,
        contract_schema_version=GOAL_SCHEMA_VERSION,
        dod_evidence=(_disposition_item(),),
        execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
    )
    result = ModelEvidenceCheckResult(
        evidence_id="child-verdict-readback",
        description="read the child verdict row",
        status=EnumEvidenceCheckStatus.VERIFIED,
        proof_class=EnumCheckProofClass.READBACK,
    )

    state, completed = HandlerDodVerify().run_verification(command, [result])
    verdict = resolve_dod_eval_outcome(
        status=completed.status,
        failed_count=completed.failed_count,
        total_checks=completed.total_checks,
        behavior_proving_count=completed.behavior_proving_count,
    )

    assert state.status.value == "verified"
    assert state.total_checks == 1
    assert state.readback_proving_count == 1
    assert state.behavior_proving_count == 0
    assert verdict.refusal is EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK


@pytest.mark.unit
def test_legacy_ticket_contract_path_remains_valid_without_goal_identity() -> None:
    command = ModelDodVerifyStartCommand(
        ticket_id="OMN-20025",
        contract_path="contracts/OMN-20025.yaml",
    )

    assert command.goal_id is None
    assert command.contract_path == "contracts/OMN-20025.yaml"
    assert "goal_id" not in command.model_dump(exclude_none=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "failed", "total", "behavior"),
    [
        ("verified", 0, 0, 0),
        ("verified", 0, 1, 0),
        ("failed", 0, 2, 1),
        ("verified", 1, 3, 1),
        ("verified", 0, 2, 1),
    ],
)
def test_market_projection_refusal_matches_released_core_reducer(
    status: str, failed: int, total: int, behavior: int
) -> None:
    core = resolve_core_dod_eval_outcome(
        ModelDodEvalInput(
            status=EnumDodEvalVerificationStatus(status),
            failed_count=failed,
            total_checks=total,
            behavior_proving_count=behavior,
        )
    )
    market = resolve_dod_eval_outcome(
        status=EnumDodVerifyStatus(status),
        failed_count=failed,
        total_checks=total,
        behavior_proving_count=behavior,
    )

    assert market.outcome.value == core.outcome.value
    assert (market.refusal.value if market.refusal is not None else None) == (
        core.refusal.value if core.refusal is not None else None
    )
