# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_record_compute (OMN-21188).

One review invocation in; one ``ModelReviewRecord`` per arm out, all under the
invocation's id, each finding wrapped in the recorded-finding envelope. The
review-recorded topic is declared in the contract and read from it.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_review_record_compute as _node_pkg
from omnimarket.enums.enum_review_execution_status import EnumReviewExecutionStatus
from omnimarket.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    EnumReviewVerdict,
    ModelFindingEvidence,
    ModelReviewFinding,
)
from omnimarket.models.review_record.model_recorded_finding import (
    ModelFindingThreadRef,
    ModelOriginalFindingRef,
    ModelRecordedFinding,
)
from omnimarket.models.review_record.model_review_record import (
    ModelReviewCost,
    ModelReviewPrincipal,
    ModelReviewRecord,
    ModelReviewServed,
    ModelReviewTarget,
)
from omnimarket.nodes.node_review_record_compute.handlers import (
    handler_review_record as _handler_module,
)
from omnimarket.nodes.node_review_record_compute.handlers.handler_review_record import (
    HandlerReviewRecordCompute,
    review_recorded_topic,
)
from omnimarket.nodes.node_review_record_compute.models.model_review_invocation import (
    ModelReviewArmResult,
    ModelReviewInvocation,
)

_NODE_DIR = Path(_node_pkg.__file__).parent
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_START = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
_TOPIC = "onex.evt.omnimarket.review-recorded.v1"


def _finding(n: int, *, defect_code: str | None = None) -> ModelReviewFinding:
    return ModelReviewFinding(
        id=uuid4(),
        category=EnumFindingCategory.LOGIC_ERROR,
        severity=EnumFindingSeverity.MAJOR,
        title=f"finding {n}",
        description=f"description {n}",
        evidence=ModelFindingEvidence(
            file_path=f"src/mod_{n}.py",
            line_range={"start": n, "end": n + 3},
            code_snippet=f"x = {n}",
        ),
        confidence=EnumReviewConfidence.HIGH,
        source_model="qwen3.8-27b",
        detection_method="llm",
        defect_code=defect_code,
    )


def _target() -> ModelReviewTarget:
    return ModelReviewTarget(
        repository="OmniNode-ai/omnimarket",
        pr_number=4200,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_digest="sha256:" + "c" * 64,
    )


def _principal() -> ModelReviewPrincipal:
    return ModelReviewPrincipal(
        principal="onexbot",
        lane="rlearn-omn-21188-1320",
        provenance="runtime-identity",
    )


def _arm(
    route: str,
    findings: list[ModelReviewFinding],
    *,
    status: EnumReviewExecutionStatus = EnumReviewExecutionStatus.COMPLETED,
    reason: str | None = None,
    verdict: EnumReviewVerdict | None = EnumReviewVerdict.RISKS_NOTED,
    served_model: str = "unknown",
) -> ModelReviewArmResult:
    return ModelReviewArmResult(
        route=route,
        requested_settings={"temperature": 0.0},
        served=ModelReviewServed(model=served_model),
        started_at=_START,
        finished_at=_START + timedelta(seconds=30),
        verdict=verdict,
        findings=findings,
        execution_status=status,
        status_reason=reason,
        cost=ModelReviewCost(wall_time_ms=30_000, prompt_tokens=1200),
    )


def _invocation(arms: list[ModelReviewArmResult]) -> ModelReviewInvocation:
    return ModelReviewInvocation(
        invocation_id=uuid4(),
        source_node="node_hostile_reviewer_orchestrator",
        prompt_template_id="hostile-v1",
        target=_target(),
        principal=_principal(),
        arms=arms,
    )


# AC1 ---------------------------------------------------------------------


@pytest.mark.parametrize("n", range(4))
def test_envelope_round_trips_finding_without_loss(n: int) -> None:
    finding = _finding(n, defect_code=("R:no-bare-except" if n % 2 else None))
    recorded = ModelRecordedFinding.from_finding(finding, finding_id=uuid4())
    assert recorded.to_finding() == finding
    assert recorded.to_finding().model_dump() == finding.model_dump()


def test_envelope_round_trips_a_fixture_of_findings_through_wire_json() -> None:
    fixture = [_finding(i, defect_code="X" if i == 2 else None) for i in range(5)]
    envelopes = [
        ModelRecordedFinding.from_finding(
            f,
            finding_id=uuid4(),
            occurrence_key=f"occ-{i}",
            defect_key=f"def-{i}",
            thread_ref=ModelFindingThreadRef(
                thread_id=f"PRRT_{i}", kind="review_thread"
            ),
            original_finding_ref=ModelOriginalFindingRef(
                finding_id=f.id, review_id=uuid4()
            ),
        )
        for i, f in enumerate(fixture)
    ]
    wire = [e.model_dump_json() for e in envelopes]
    back = [ModelRecordedFinding.model_validate_json(w) for w in wire]
    assert back == envelopes
    assert [b.to_finding() for b in back] == fixture


def test_envelope_keeps_the_wrapped_findings_own_id_apart_from_its_emission_id() -> (
    None
):
    finding = _finding(1)
    emission = uuid4()
    recorded = ModelRecordedFinding.from_finding(finding, finding_id=emission)
    assert recorded.finding_id == emission
    assert recorded.to_finding().id == finding.id
    assert recorded.finding_id != finding.id


def test_envelope_optional_keys_default_to_unset() -> None:
    recorded = ModelRecordedFinding.from_finding(_finding(1), finding_id=uuid4())
    assert recorded.defect_key is None
    assert recorded.occurrence_key is None
    assert recorded.thread_ref is None
    assert recorded.original_finding_ref is None


def test_envelope_refuses_unknown_fields() -> None:
    payload = ModelRecordedFinding.from_finding(
        _finding(1), finding_id=uuid4()
    ).model_dump(mode="json")
    payload["severity_override"] = "nit"
    with pytest.raises(ValidationError):
        ModelRecordedFinding.model_validate(payload)


# AC2 ---------------------------------------------------------------------


def test_two_arm_invocation_writes_two_records_under_one_invocation_id() -> None:
    invocation = _invocation(
        [
            _arm("lab-qwen", [_finding(1), _finding(2)]),
            _arm("cloud-glm", [_finding(3)], served_model="glm-5.3"),
        ]
    )
    records = HandlerReviewRecordCompute().handle(invocation)

    assert len(records) == 2
    assert all(isinstance(r, ModelReviewRecord) for r in records)
    assert {r.invocation_id for r in records} == {invocation.invocation_id}
    assert len({r.review_id for r in records}) == 2
    assert [r.arm.route for r in records] == ["lab-qwen", "cloud-glm"]


def test_each_finding_is_attributed_to_exactly_one_arm() -> None:
    a_findings = [_finding(1), _finding(2)]
    b_findings = [_finding(3)]
    invocation = _invocation(
        [_arm("lab-qwen", a_findings), _arm("cloud-glm", b_findings)]
    )
    first, second = HandlerReviewRecordCompute().handle(invocation)
    assert [f.to_finding() for f in first.findings] == a_findings
    assert [f.to_finding() for f in second.findings] == b_findings
    emission_ids = [f.finding_id for r in (first, second) for f in r.findings]
    assert len(set(emission_ids)) == 3
    for record in (first, second):
        for f in record.findings:
            assert f.original_finding_ref is not None
            assert f.original_finding_ref.finding_id == f.to_finding().id
            assert f.original_finding_ref.review_id == record.review_id


def test_record_carries_invocation_context_and_arm_provenance() -> None:
    invocation = _invocation(
        [_arm("lab-qwen", [_finding(1)], served_model="qwen3.8-27b")]
    )
    (record,) = HandlerReviewRecordCompute().handle(invocation)
    assert record.source_node == "node_hostile_reviewer_orchestrator"
    assert record.prompt_template_id == "hostile-v1"
    assert record.rubric_hash == "none"
    assert record.target == invocation.target
    assert record.principal == invocation.principal
    assert record.arm.requested_settings == {"temperature": 0.0}
    assert record.arm.served.model == "qwen3.8-27b"
    assert record.cost.wall_time_ms == 30_000
    assert record.verdict is EnumReviewVerdict.RISKS_NOTED
    assert record.execution_status is EnumReviewExecutionStatus.COMPLETED


def test_served_model_defaults_to_explicit_unknown() -> None:
    (record,) = HandlerReviewRecordCompute().handle(_invocation([_arm("r", [])]))
    assert record.arm.served.model == "unknown"
    assert record.arm.served.settings is None


def test_failed_and_partial_arms_still_write_their_own_record() -> None:
    invocation = _invocation(
        [
            _arm("ok", [_finding(1)]),
            _arm(
                "down",
                [],
                status=EnumReviewExecutionStatus.FAILED,
                reason="route timed out",
                verdict=None,
            ),
            _arm(
                "cut",
                [_finding(2)],
                status=EnumReviewExecutionStatus.PARTIAL,
                reason="response truncated",
            ),
        ]
    )
    records = HandlerReviewRecordCompute().handle(invocation)
    assert [r.execution_status for r in records] == [
        EnumReviewExecutionStatus.COMPLETED,
        EnumReviewExecutionStatus.FAILED,
        EnumReviewExecutionStatus.PARTIAL,
    ]
    assert records[1].status_reason == "route timed out"
    assert records[1].findings == ()
    assert len({r.invocation_id for r in records}) == 1


def test_handler_is_deterministic_so_a_redelivery_yields_the_same_records() -> None:
    invocation = _invocation([_arm("a", [_finding(1)]), _arm("b", [_finding(2)])])
    handler = HandlerReviewRecordCompute()
    assert handler.handle(invocation) == handler.handle(invocation)


def test_invocation_refuses_two_arms_on_one_route() -> None:
    with pytest.raises(ValidationError, match="route"):
        _invocation([_arm("same", [_finding(1)]), _arm("same", [_finding(2)])])


def test_invocation_refuses_no_arms() -> None:
    with pytest.raises(ValidationError):
        _invocation([])


def test_non_completed_arm_must_state_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        _arm("down", [], status=EnumReviewExecutionStatus.FAILED, verdict=None)


def test_arm_refuses_finish_before_start() -> None:
    with pytest.raises(ValidationError, match="finished_at"):
        ModelReviewArmResult(
            route="r",
            requested_settings={},
            started_at=_START,
            finished_at=_START - timedelta(seconds=1),
            verdict=EnumReviewVerdict.CLEAN,
            findings=[],
            execution_status=EnumReviewExecutionStatus.COMPLETED,
            cost=ModelReviewCost(wall_time_ms=0),
        )


def test_record_cost_has_no_price_field() -> None:
    with pytest.raises(ValidationError):
        ModelReviewCost.model_validate({"wall_time_ms": 1, "price_usd": 0.01})


def test_unavailable_target_states_why_and_never_carries_shas() -> None:
    with pytest.raises(ValidationError, match="unavailable"):
        ModelReviewTarget(
            repository="OmniNode-ai/omnimarket", pr_number=1, snapshot_available=False
        )
    with pytest.raises(ValidationError, match="unavailable"):
        ModelReviewTarget(
            repository="OmniNode-ai/omnimarket",
            pr_number=1,
            snapshot_available=False,
            unavailable_reason="diff effect returned no head sha",
            head_sha="a" * 40,
        )
    target = ModelReviewTarget(
        repository="OmniNode-ai/omnimarket",
        pr_number=1,
        snapshot_available=False,
        unavailable_reason="diff effect returned no head sha",
    )
    assert target.head_sha is None


def test_available_target_requires_the_whole_snapshot() -> None:
    with pytest.raises(ValidationError, match="snapshot"):
        ModelReviewTarget(
            repository="OmniNode-ai/omnimarket", pr_number=1, head_sha="a" * 40
        )


def test_record_is_a_frozen_wire_model_that_round_trips() -> None:
    invocation = _invocation([_arm("a", [_finding(1)])])
    (record,) = HandlerReviewRecordCompute().handle(invocation)
    assert ModelReviewRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        record.__setattr__("verdict", EnumReviewVerdict.CLEAN)


# AC3 ---------------------------------------------------------------------


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        _CONTRACT_PATH.read_text(encoding="utf-8")
    )
    return contract


def test_review_recorded_topic_is_declared_in_the_contract() -> None:
    contract = _contract()
    assert contract["event_bus"]["publish_topics"] == [_TOPIC]


def test_handler_topic_is_the_contracts_topic() -> None:
    declared = _contract()["event_bus"]["publish_topics"]
    assert [review_recorded_topic()] == declared
    assert HandlerReviewRecordCompute().topic == declared[0]


def test_topic_is_not_spelt_in_handler_or_node_code() -> None:
    needle = "review-recorded"
    offenders = [
        str(p.relative_to(_NODE_DIR))
        for p in sorted(_NODE_DIR.rglob("*.py"))
        if needle in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert needle not in inspect.getsource(_handler_module)


def test_handler_reads_a_changed_contract_topic(tmp_path: Path) -> None:
    contract = _contract()
    contract["event_bus"]["publish_topics"] = ["onex.evt.omnimarket.other.v1"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    assert review_recorded_topic(path) == "onex.evt.omnimarket.other.v1"


def test_contract_names_the_models_and_handler_it_runs() -> None:
    contract = _contract()
    assert contract["name"] == "node_review_record_compute"
    assert contract["node_type"] == "compute"
    handler = contract["handler"]
    assert handler["class"] == "HandlerReviewRecordCompute"
    assert contract["input_model"]["name"] == "ModelReviewInvocation"
