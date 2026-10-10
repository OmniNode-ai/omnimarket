# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run terminals retain refusals and fail closed on replay failures."""

from collections import Counter
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_result import (
    ModelDelegationGateEvalResult,
)
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers import (
    HandlerDelegationEvalRun,
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
        manifest_id="edge-manifest",
        rater_role="human",
        rubric_version="v1",
        gate_version="gate-1",
    )


def _items() -> tuple[ModelGateEvalItem, ...]:
    return tuple(
        ModelGateEvalItem(
            item_id=f"call-{index}:0",
            task_class="summarization",
            stratum="summarization/accepted",
            label="adequate",
            prompt_text="Summarize the supplied facts.",
            recorded_answer="The supplied facts agree.",
            recorded_verdict="accepted",
        )
        for index in (2, 0, 1)
    )


def _source(items: tuple[ModelGateEvalItem, ...]) -> Mock:
    source = Mock(spec=ProtocolDelegationEvalLabelledItems)
    source.get_labelled_items.return_value = items
    return source


def test_all_items_pass_returns_completed_terminal(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    source = _source(_items())
    replay = Mock(return_value=ModelGateReplayVerdict(verdict="accepted"))
    handler = HandlerDelegationEvalRun(
        source, HandlerDelegationGateEval(replay), clock=lambda: _OBSERVED_AT
    )

    payload = handler.handle(run_request).payload

    source.get_labelled_items.assert_called_once_with("edge-manifest", "human", "v1")
    assert payload.status == "completed"
    assert payload.failure_reasons == ()
    assert payload.tenant_id == run_request.tenant_id
    assert payload.manifest_id == run_request.manifest_id
    assert payload.gate_version == run_request.gate_version
    assert payload.observed_at == _OBSERVED_AT
    assert [row.item_key for row in payload.item_verdicts] == [
        "call-0:0",
        "call-1:0",
        "call-2:0",
    ]
    assert all(row.replayed_verdict == "accepted" for row in payload.item_verdicts)
    assert all(row.replay_count == 3 for row in payload.item_verdicts)
    assert Counter(call.args[0].item_id for call in replay.call_args_list) == {
        "call-0:0": 3,
        "call-1:0": 3,
        "call-2:0": 3,
    }
    gate_rows = [row for row in payload.results if row.arm != "rubric"]
    assert len(gate_rows) == 4
    # Summarization has a class rubric, so the run also reports its rubric arm.
    assert {row.stratum for row in payload.results if row.arm == "rubric"} == {
        "all",
        "summarization/accepted",
    }
    assert all(row.total_n == row.accepted_n == 3 for row in gate_rows)
    assert all(row.false_pass_rate == 0.0 for row in gate_rows)
    assert all(row.false_refusal_rate is None for row in gate_rows)
    assert payload == handler.handle(run_request).payload


def test_one_refused_item_is_a_completed_evaluation_with_refusal_evidence(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    def replay(item: ModelGateEvalItem) -> ModelGateReplayVerdict:
        if item.item_id == "call-1:0":
            return ModelGateReplayVerdict(
                verdict="refused", deciding_check="numbers_grounded"
            )
        return ModelGateReplayVerdict(verdict="accepted")

    # Also exercise serialization when the original verdict was not recorded.
    items = tuple(
        item.model_copy(update={"recorded_verdict": None})
        if item.item_id == "call-1:0"
        else item
        for item in _items()
    )
    payload = (
        HandlerDelegationEvalRun(_source(items), HandlerDelegationGateEval(replay))
        .handle(run_request)
        .payload
    )

    assert payload.status == "completed"
    assert payload.failure_reasons == ()
    refused = next(row for row in payload.item_verdicts if row.item_key == "call-1:0")
    assert refused.recorded_verdict is None
    assert refused.replayed_verdict == "refused"
    assert refused.replayed_deciding_check == "numbers_grounded"
    assert refused.replay_count == 3
    (row,) = (
        row for row in payload.results if row.arm == "replayed" and row.stratum == "all"
    )
    assert row.total_n == 3
    assert row.accepted_n == 2
    assert row.refused_n == row.false_refusal_count == 1
    assert row.false_refusal_rate == 1.0


def test_failed_gate_terminal_preserves_model_timeout_reason(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    # A gate adapter may return a failed result. The orchestrator must preserve
    # that terminal rather than manufacturing completed rows from partial work.
    gate = Mock(spec=HandlerDelegationGateEval)
    gate.handle.side_effect = lambda command: ModelDelegationGateEvalResult(
        run_id=command.run_id,
        status="failed",
        failure_reasons=("model timeout for call-1:0",),
    )
    payload = (
        HandlerDelegationEvalRun(_source(_items()), gate, clock=lambda: _OBSERVED_AT)
        .handle(run_request)
        .payload
    )

    assert payload.status == "failed"
    assert payload.failure_reasons == ("model timeout for call-1:0",)
    assert payload.item_verdicts == payload.results == ()
    assert payload.observed_at == _OBSERVED_AT
    gate.handle.assert_called_once()
    (command,) = gate.handle.call_args.args
    assert command.run_id == str(payload.eval_run_id)
    assert [item.item_id for item in command.items] == [
        "call-0:0",
        "call-1:0",
        "call-2:0",
    ]


def test_unhandled_model_timeout_propagates_without_success_terminal(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    # The pure replay handler does not catch transport/model exceptions.
    replay = Mock(side_effect=TimeoutError("model deadline exceeded"))
    handler = HandlerDelegationEvalRun(
        _source(_items()), HandlerDelegationGateEval(replay)
    )

    with pytest.raises(TimeoutError, match=r"^model deadline exceeded$"):
        handler.handle(run_request)

    replay.assert_called_once()


def test_empty_manifest_fails_without_replaying(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    gate = Mock(spec=HandlerDelegationGateEval)
    payload = HandlerDelegationEvalRun(_source(()), gate).handle(run_request).payload

    assert payload.status == "failed"
    assert payload.failure_reasons == (
        "no labelled items for manifest edge-manifest under rater human rubric v1",
    )
    assert payload.item_verdicts == payload.results == ()
    gate.handle.assert_not_called()


def test_missing_labelled_item_source_refuses_before_replaying(
    run_request: ModelDelegationEvalRunRequest,
) -> None:
    gate = Mock(spec=HandlerDelegationGateEval)

    with pytest.raises(
        RuntimeError, match=r"^tenant-scoped labelled-item source is required$"
    ):
        HandlerDelegationEvalRun(gate_eval=gate).handle(run_request)

    gate.handle.assert_not_called()
