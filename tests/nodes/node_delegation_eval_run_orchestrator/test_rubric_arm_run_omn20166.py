# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20166: run orchestration measures class rubrics against labels."""

from datetime import UTC, datetime
from unittest.mock import Mock, call
from uuid import uuid5

import pytest

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    EVAL_RUN_NAMESPACE,
    eval_run_id,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.protocols.protocol_delegation_eval_labelled_items import (
    ProtocolDelegationEvalLabelledItems,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)

pytestmark = pytest.mark.unit
_OBSERVED_AT = datetime(2026, 9, 28, tzinfo=UTC)


@pytest.fixture
def run_request() -> ModelDelegationEvalRunRequest:
    return ModelDelegationEvalRunRequest(
        tenant_id="11111111-1111-1111-1111-111111111111",
        manifest_id="rubric-manifest",
        rater_role="human",
        rubric_version="v1",
        gate_version="gate-1",
    )


def _item(
    index: int,
    *,
    task_class: str = "summarization",
    label: str = "adequate",
    answer: str | None = "OMN-20166 wires rubric evaluation.",
) -> ModelGateEvalItem:
    return ModelGateEvalItem(
        item_id=f"call-{index}:0",
        task_class=task_class,
        stratum=f"{task_class}/accepted",
        label=label,
        prompt_text="Summarize: OMN-20166 wires rubric evaluation.",
        recorded_answer=answer,
        recorded_verdict="accepted",
    )


def _source(items: tuple[ModelGateEvalItem, ...]) -> Mock:
    source = Mock(spec=ProtocolDelegationEvalLabelledItems)
    source.get_labelled_items.return_value = items
    return source


def _gate() -> Mock:
    replay = Mock(return_value=ModelGateReplayVerdict(verdict="accepted"))
    return Mock(spec=HandlerDelegationGateEval, wraps=HandlerDelegationGateEval(replay))


def test_rubric_arm_run_scores_rubric_verdicts_against_labels(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    contract = load_delegation_class_rubrics()
    loader = Mock(return_value=contract)
    items = (
        _item(3, label="inadequate", answer="fail-inadequate"),
        _item(1, label="inadequate", answer="pass-inadequate"),
        _item(2, answer="fail-adequate"),
        _item(0, answer="pass-adequate"),
        _item(4, task_class="reasoning"),
    )

    def verdict(
        *, task_class: str, request_text: str, answer_text: str
    ) -> ModelAttemptRubricVerdict:
        return ModelAttemptRubricVerdict(
            rubric_version=contract.rubric_version,
            task_class=task_class,
            outcome="PASS" if answer_text.startswith("pass") else "FAIL",
        )

    rubric = Mock(side_effect=verdict)
    gate = _gate()
    payload = (
        HandlerDelegationEvalRun(
            _source(items), gate, class_rubrics=loader, rubric_verdict=rubric
        )
        .handle(run_request)
        .payload
    )

    assert payload.status == "completed"
    loader.assert_called_once_with()
    assert rubric.call_args_list == [
        call(
            task_class=item.task_class,
            request_text=item.prompt_text,
            answer_text=item.recorded_answer,
        )
        for item in sorted(items, key=lambda item: item.item_id)
        if item.task_class == "summarization"
    ]
    rows = [row for row in payload.results if row.arm == "rubric"]
    assert {row.task_class for row in rows} == {"summarization"}
    assert {row.stratum for row in rows} == {"all", "summarization/accepted"}
    for row in rows:
        assert row.total_n == 4
        assert row.accepted_n == 2
        assert row.false_pass_count == 1
        assert row.refused_n == 2
        assert row.false_refusal_count == 1
        assert row.false_pass_rate == row.false_refusal_rate == 0.5
    (command,) = gate.handle.call_args.args
    assert command.rubric_false_pass_lines == {
        "summarization": contract.false_pass_lines["summarization"]
    }
    assert command.rubric_false_refusal_lines == {
        "summarization": contract.false_refusal_lines["summarization"]
    }
    assert command.items[-1] is items[-1]


def test_rubric_arm_run_class_without_rubric_has_no_rubric_rows(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    items = (_item(1, task_class="reasoning"), _item(0, task_class="reasoning"))
    rubric = Mock()
    gate = _gate()
    payload = (
        HandlerDelegationEvalRun(_source(items), gate, rubric_verdict=rubric)
        .handle(run_request)
        .payload
    )

    assert payload.status == "completed"
    assert not any(row.arm == "rubric" for row in payload.results)
    rubric.assert_not_called()
    (command,) = gate.handle.call_args.args
    assert command.rubric_false_pass_lines == command.rubric_false_refusal_lines == {}
    assert command.items == tuple(reversed(items))
    assert payload.eval_run_id == eval_run_id(
        run_request.manifest_id, payload.label_set_sha256, run_request.gate_version
    )
    assert payload.eval_run_id == uuid5(
        EVAL_RUN_NAMESPACE,
        f"{run_request.manifest_id}\n{payload.label_set_sha256}\n{run_request.gate_version}",
    )


def test_rubric_arm_run_missing_answer_counts_against_line(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    contract = load_delegation_class_rubrics()
    items = (_item(0), _item(1, answer=None))
    rubric = Mock(
        return_value=ModelAttemptRubricVerdict(
            rubric_version=contract.rubric_version,
            task_class="summarization",
            outcome="PASS",
        )
    )
    gate = _gate()
    payload = (
        HandlerDelegationEvalRun(_source(items), gate, rubric_verdict=rubric)
        .handle(run_request)
        .payload
    )

    assert payload.status == "completed"
    rubric.assert_called_once_with(
        task_class="summarization",
        request_text=items[0].prompt_text,
        answer_text=items[0].recorded_answer,
    )
    (row,) = (
        row for row in payload.results if row.arm == "rubric" and row.stratum == "all"
    )
    assert row.total_n == 2
    assert row.accepted_n == 1
    assert row.false_pass_count == 0
    assert row.false_pass_wilson_low > 0.0
    assert row.false_pass_line_verdict != "met"
    assert len(payload.item_verdicts) == 2
    (command,) = gate.handle.call_args.args
    assert command.items[1] is items[1]
    assert command.items[1].rubric_verdict is None


def test_rubric_arm_run_rubric_version_is_part_of_run_id(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    base_contract = load_delegation_class_rubrics()
    items = (_item(0),)
    payloads = []
    for version in ("x", "y", "x"):
        contract = base_contract.model_copy(update={"rubric_version": version})
        rubric = Mock(
            return_value=ModelAttemptRubricVerdict(
                rubric_version=contract.rubric_version,
                task_class="summarization",
                outcome="PASS",
            )
        )
        payload = (
            HandlerDelegationEvalRun(
                _source(items),
                _gate(),
                clock=lambda: _OBSERVED_AT,
                class_rubrics=Mock(return_value=contract),
                rubric_verdict=rubric,
            )
            .handle(run_request)
            .payload
        )
        assert payload.status == "completed"
        assert payload.eval_run_id == eval_run_id(
            run_request.manifest_id,
            payload.label_set_sha256,
            run_request.gate_version,
            version,
        )
        assert payload.eval_run_id == uuid5(
            EVAL_RUN_NAMESPACE,
            f"{run_request.manifest_id}\n{payload.label_set_sha256}\n"
            f"{run_request.gate_version}\n{version}",
        )
        payloads.append(payload)

    assert len({payload.label_set_sha256 for payload in payloads}) == 1
    assert payloads[0].eval_run_id != payloads[1].eval_run_id
    assert payloads[0] == payloads[2]


@pytest.mark.parametrize("exception_type", [ValueError, OSError])
def test_rubric_arm_run_contract_unavailable_fails_run(
    run_request: ModelDelegationEvalRunRequest,
    exception_type: type[Exception],
) -> None:
    item = _item(0)
    loader = Mock(
        side_effect=exception_type(item.prompt_text + str(item.recorded_answer))
    )
    gate = _gate()
    rubric = Mock()
    payload = (
        HandlerDelegationEvalRun(
            _source((item,)),
            gate,
            class_rubrics=loader,
            rubric_verdict=rubric,
        )
        .handle(run_request)
        .payload
    )

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        f"class rubric contract unavailable: {exception_type.__name__}",
    )
    assert payload.results == payload.item_verdicts == ()
    loader.assert_called_once_with()
    gate.handle.assert_not_called()
    rubric.assert_not_called()


def test_rubric_arm_run_real_rubric_end_to_end(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    contract = load_delegation_class_rubrics()
    items = (_item(0),)
    gate = _gate()
    payload = HandlerDelegationEvalRun(_source(items), gate).handle(run_request).payload

    assert payload.status == "completed"
    (row,) = (
        row
        for row in payload.results
        if row.arm == "rubric"
        and row.task_class == "summarization"
        and row.stratum == "all"
    )
    assert row.total_n == row.accepted_n == 1
    assert [verdict.item_key for verdict in payload.item_verdicts] == [
        item.item_id for item in items
    ]
    (command,) = gate.handle.call_args.args
    assert all(item.rubric_verdict is not None for item in command.items)
    assert command.items[0].rubric_verdict.rubric_version == contract.rubric_version
    assert command.items[0].rubric_verdict.outcome == "PASS"


def test_rubric_arm_run_empty_manifest_does_not_load_contract(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    loader = Mock(side_effect=ValueError("contract unavailable"))
    gate = _gate()
    payload = (
        HandlerDelegationEvalRun(_source(()), gate, class_rubrics=loader)
        .handle(run_request)
        .payload
    )

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "no labelled items for manifest rubric-manifest under rater human rubric v1",
    )
    assert payload.eval_run_id == eval_run_id(
        run_request.manifest_id, payload.label_set_sha256, run_request.gate_version
    )
    loader.assert_not_called()
    gate.handle.assert_not_called()
