# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20552: a document answer is checked against the facts it was given.

The fixture's recorded run is onex delegate run
de6357ab-d4e3-42de-b6a9-3ba186160860 verbatim: a document answer the gate
scored 1.0 while its rubric recorded UNDETERMINED no_rubric_for_class. The
document class now records claims_traceable, which refuses an identifier,
ref, path, quote or number the facts do not carry. That run's two invented
causes reuse the facts' own words, so no deterministic check refuses it; the
offline acceptance judge does, and its recorded reply is replayed here
through the judge node's own render and check operations.
"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.rubric import attempt_verdict
from omnimarket.delegation.rubric.attempt_verdict import (
    apply_measured_rubric,
    record_attempt_rubric_verdict,
)
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelRubricCheckRequest,
    ModelRubricVerdict,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = (
    _ROOT
    / "tests"
    / "fixtures"
    / "delegation"
    / "omn20552"
    / "document_facts_grounding.json"
)
_JUDGE_RUBRIC = (
    _ROOT
    / "src"
    / "omnimarket"
    / "configs"
    / "delegation_acceptance_judge_rubric.v1.yaml"
)
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_RUN = _CASES["recorded_run"]
_REFUSED = _CASES["rubric_refused"]
_PASSED = _CASES["rubric_passed"]
_REPLAY = _CASES["judge_replay"]


def _verdict(case: dict[str, str]) -> ModelRubricVerdict:
    contract = load_delegation_class_rubrics()
    return HandlerDelegationRubricCheck().handle(
        ModelRubricCheckRequest(
            task_class=case["task_class"],
            request_text=case["request_text"],
            answer_text=case["answer_text"],
            rubric=contract.for_class(case["task_class"]),
        )
    )


def _with_document_status(
    monkeypatch: pytest.MonkeyPatch, status: EnumRangeVerdict
) -> None:
    contract = load_delegation_class_rubrics()
    raw = contract.model_dump(mode="json")
    raw["false_pass_status"]["document"] = status.value
    configured = type(contract).model_validate(raw)
    monkeypatch.setattr(attempt_verdict, "_load_contract", lambda: configured)


def test_document_facts_grounding_fixture_is_the_recorded_run():
    provenance = _CASES["provenance"]
    assert provenance["run_id"] == "de6357ab-d4e3-42de-b6a9-3ba186160860"
    assert provenance["quality_score"] == 1.0
    assert provenance["recorded_rubric_verdict"]["undetermined_criteria"] == [
        "no_rubric_for_class"
    ]
    assert _REPLAY["cases"][0]["answer_text"] == _RUN["answer_text"]


def test_document_facts_grounding_contract_declares_document_non_met():
    contract = load_delegation_class_rubrics()
    assert [row.criterion_id for row in contract.classes["document"]] == [
        "claims_traceable"
    ]
    # document checks its facts with the summarization class's own criterion.
    assert contract.classes["document"][0] == contract.classes["summarization"][0]
    assert contract.false_pass_status["document"] is not EnumRangeVerdict.MET


def test_document_facts_grounding_recorded_run_no_longer_unrubricked():
    verdict = _verdict(_RUN)
    assert verdict.criteria[0].reason_code != "no_rubric_for_class"
    # Every identifier, ref, path and number in it is in the facts, so the
    # deterministic rubric records a pass: the false pass the class's
    # false-pass line counts once the judge has labelled it.
    assert verdict.outcome == "PASS"


@pytest.mark.parametrize("case", _REFUSED, ids=[row["case_id"] for row in _REFUSED])
def test_document_facts_grounding_refuses_unsupported_identifiers(case):
    verdict = _verdict(case)
    assert verdict.outcome == "FAIL"
    assert verdict.failed_criteria == ("claims_traceable",)
    assert verdict.criteria[0].reason_code == "sentence_untraceable"
    assert set(case["missing"]) <= set(verdict.criteria[0].facts)


@pytest.mark.parametrize("case", _PASSED, ids=[row["case_id"] for row in _PASSED])
def test_document_facts_grounding_passes_grounded_answers(case):
    assert _verdict(case).outcome == "PASS"


def test_document_facts_grounding_recorded_on_the_attempt_path():
    case = _REFUSED[0]
    recorded = record_attempt_rubric_verdict(
        task_class="document",
        request_text=case["request_text"],
        answer_text=case["answer_text"],
    )
    assert recorded.outcome == "FAIL"
    assert recorded.failed_criteria == ("claims_traceable",)


@pytest.mark.parametrize(
    ("status", "refused"),
    [(EnumRangeVerdict.REFUSED, False), (EnumRangeVerdict.MET, True)],
)
def test_document_facts_grounding_gate_refuses_only_when_document_met(
    monkeypatch, status, refused
):
    _with_document_status(monkeypatch, status)
    case = _REFUSED[0]
    recorded = record_attempt_rubric_verdict(
        task_class="document",
        request_text=case["request_text"],
        answer_text=case["answer_text"],
    )
    accepted_by_floor = ModelQualityGateResult(
        correlation_id=uuid4(), passed=True, quality_score=1.0
    )
    decided = apply_measured_rubric(accepted_by_floor, recorded, task_class="document")
    assert decided.passed is not refused
    if refused:
        assert decided.fail_category == "rubric_failed"
        assert decided.failure_reasons == ("rubric_failed: claims_traceable",)


def test_document_facts_grounding_judge_replay_rejects_the_recorded_run():
    rubric_yaml = _JUDGE_RUBRIC.read_text(encoding="utf-8")
    judge = HandlerDelegationAcceptanceJudge()
    rendered = judge.handle(
        ModelAcceptanceJudgeRequest(
            operation=EnumAcceptanceOperation.RENDER,
            rubric_yaml=rubric_yaml,
            seed=_REPLAY["seed"],
            items=tuple(
                ModelAcceptanceItem(
                    item_id=case["judge_item_id"],
                    model="withheld",
                    task_type=case["task_class"],
                    task_text=case["request_text"],
                    answer_text=case["answer_text"],
                )
                for case in _REPLAY["cases"]
            ),
        )
    )
    batch = rendered.batches[0]
    # The recorded reply answers exactly this prompt.
    assert hashlib.sha256(batch.prompt.encode()).hexdigest() == _REPLAY["prompt_sha256"]
    checked = judge.handle(
        ModelAcceptanceJudgeRequest(
            operation=EnumAcceptanceOperation.CHECK,
            rubric_yaml=rubric_yaml,
            batch_item_ids=batch.item_ids,
            reply_text=_REPLAY["reply_text"],
        )
    )
    assert checked.rubric_version == _REPLAY["rubric_version"]
    assert checked.issues == ()
    case_ids = {case["judge_item_id"]: case["case_id"] for case in _REPLAY["cases"]}
    by_case = {case_ids[row.item_id]: row for row in checked.verdicts}
    assert by_case["de6357ab_document"].accept is False
    assert by_case["de6357ab_document"].failure_class == "fabrication"
    assert by_case["de6357ab_document_grounded"].accept is True
    # The replay is of the document class's false pass: the judge rejects the
    # recorded run while the deterministic document rubric records claims_traceable
    # PASS on the same text. Without the document class that rubric records no
    # criterion for it, so this leg fails where the class does not exist.
    for case in _REPLAY["cases"]:
        if case["task_class"] != "document":
            continue
        document_verdict = _verdict(case)
        assert [row.criterion_id for row in document_verdict.criteria] == [
            "claims_traceable"
        ]
    assert _verdict(_REPLAY["cases"][0]).outcome == "PASS"
    assert _verdict(_REPLAY["cases"][2]).outcome == "PASS"


async def _dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: dict[str, str]
) -> dict[str, object]:
    """One document case through the in-process port onex delegate uses."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as local_module,
    )
    from omnimarket.routing import delegation_backend_resolution
    from tests.unit.delegation.test_local_port_response_contract_evidence_omn19201 import (
        _backends,
        _RecordingEffect,
    )

    backends = _backends()
    backends[0].update(
        endpoint_url="https://synthetic.invalid/v1/chat/completions",
        model_name="synthetic-model",
    )
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )
    # A served response carries the port's extraction marker ahead of the
    # deliverable (the recorded run's preamble_chars is 11).
    effect = _RecordingEffect("### ANSWER\n" + case["answer_text"])
    port = local_module.LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "synthetic.sqlite",
        effect_process_boundary=False,
    )
    return await port.dispatch(
        prompt=case["request_text"],
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=60,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
        backend_id="local-coder",
        response_contract=None,
    )


@pytest.mark.asyncio
async def test_document_facts_grounding_local_port_records_the_recorded_run(
    tmp_path, monkeypatch
):
    result = await _dispatch(tmp_path, monkeypatch, _RUN)
    attempt = result["attempts"][0]
    assert result["status"] == "completed"
    assert attempt["rubric_verdict"]["task_class"] == "document"
    assert attempt["rubric_verdict"]["outcome"] == "PASS"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "refused"),
    [(EnumRangeVerdict.REFUSED, False), (EnumRangeVerdict.MET, True)],
)
async def test_document_facts_grounding_local_port_decides_only_when_met(
    tmp_path, monkeypatch, status, refused
):
    _with_document_status(monkeypatch, status)
    result = await _dispatch(tmp_path, monkeypatch, _REFUSED[0])
    attempt = result["attempts"][0]
    assert attempt["rubric_verdict"]["outcome"] == "FAIL"
    assert attempt["rubric_verdict"]["failed_criteria"] == ["claims_traceable"]
    if refused:
        assert result["status"] == "failed"
        assert attempt["failure_class"] == "rubric_failed"
        assert "claims_traceable" in attempt["acceptance_detail"]
    else:
        # Shipped config: recorded only, the floor still decides.
        assert result["status"] == "completed"
        assert attempt["failure_class"] is None
