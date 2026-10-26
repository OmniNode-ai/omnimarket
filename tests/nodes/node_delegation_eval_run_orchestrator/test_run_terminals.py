# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run terminals retain refusals and fail closed on replay failures, and the run
reproduces the committed first-run baseline (OMN-19793)."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

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
from omnimarket.nodes.node_projection_delegation_eval.handlers import (
    DelegationEvalProjectionWriter,
)
from tests.test_golden_chain_projection_delegation_eval import _RunStore

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


# --- OMN-19793: the eval run reproduces the committed first-run baseline ---
# OMN-19793: the eval run reproduces the committed first-run baseline.
#
# tests/fixtures/delegation_eval/omn19793_first_run_verdicts.json holds the content-free verdicts of the EV.4 first run (manifest
# 4960f691…a5c6, blind opus labels under ev4-blind-v1), as the dev lane stored
# them. Run through the eval run handler, they give back the same label-set
# digest and eval run id, and the recorded-verdict arm gives back, class by class,
# the false-pass and false-refusal counts that
# reports/2026-09-30-delegation-gate-false-pass-first-run.md publishes (AC1).
# Written through the rule-7a writer, the same run redelivered writes the same
# rows and no more (AC2), and the writer never reports zero rows for it (AC3).

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/delegation_eval/omn19793_first_run_verdicts.json"
)
_FIRST_RUN_ID = UUID("735b18b8-734e-5c7d-bc00-47926ac70963")

# The report's per-class table: accepted n and false passes, recorded-verdict arm.
_REPORT_FALSE_PASSES = {
    "code_review": (20, 16),
    "document": (20, 10),
    "planning": (20, 10),
    "research": (20, 7),
    "review": (20, 7),
    "reasoning": (19, 4),
    "summarization": (20, 3),
    "code_generation": (19, 2),
    "test": (20, 1),
}
# "Of 90 refused answers, 25 (27.8%) were labelled adequate."
_REPORT_REFUSED, _REPORT_FALSE_REFUSALS = 90, 25


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data


def _payload(at: datetime = datetime(2026, 9, 30, tzinfo=UTC)) -> Any:
    data = _fixture()
    items = tuple(
        ModelGateEvalItem(
            item_id=row["item_key"],
            task_class=row["task_class"],
            stratum=row["stratum"],
            label=row["label"],
            prompt_text="",
            recorded_verdict=row["recorded_verdict"],
            recorded_deciding_check=row["recorded_deciding_check"],
        )
        for row in data["items"]
    )
    source = Mock(spec=ProtocolDelegationEvalLabelledItems)
    source.get_labelled_items.return_value = items
    # The first run predates the class rubrics, so it ran with none.
    no_rubrics = SimpleNamespace(
        rubric_version="none",
        classes={},
        false_pass_lines={},
        false_refusal_lines={},
    )
    handler = HandlerDelegationEvalRun(
        source,
        HandlerDelegationGateEval(
            Mock(return_value=ModelGateReplayVerdict(verdict="accepted"))
        ),
        clock=lambda: at,
        class_rubrics=lambda: no_rubrics,
    )
    run = data["run"]
    return handler.handle(
        ModelDelegationEvalRunRequest(
            tenant_id="820272f9-4aaf-5add-a2df-0af942852ab2",
            manifest_id=run["manifest_id"],
            rater_role=run["rater_role"],
            rubric_version=run["rubric_version"],
            gate_version=run["gate_version"],
        )
    ).payload


def test_first_run_baseline_reproduces_the_label_set_and_run_id() -> None:
    payload = _payload()
    assert payload.status == "completed"
    assert payload.label_set_sha256 == _fixture()["run"]["label_set_sha256"]
    assert payload.eval_run_id == _FIRST_RUN_ID


def test_first_run_baseline_recorded_arm_matches_the_report_per_class() -> None:
    rows = [
        row
        for row in _payload().results
        if row.arm == "recorded" and row.stratum == "all"
    ]
    false_passes = {
        row.task_class: (row.accepted_n, row.false_pass_count) for row in rows
    }
    assert false_passes == _REPORT_FALSE_PASSES
    assert sum(fp for _, fp in false_passes.values()) == 60
    assert sum(n for n, _ in false_passes.values()) == 178
    assert sum(row.refused_n for row in rows) == _REPORT_REFUSED
    assert sum(row.false_refusal_count for row in rows) == _REPORT_FALSE_REFUSALS


def _writer() -> tuple[DelegationEvalProjectionWriter, _RunStore]:
    writer = DelegationEvalProjectionWriter()
    store = _RunStore()
    writer.__dict__["_db"] = store
    return writer, store


def test_first_run_baseline_redelivered_run_writes_the_same_rows_and_no_more() -> None:
    writer, store = _writer()
    first = _payload().model_dump(mode="json")
    writer.handle(first)
    tables = {t: dict(rows) for t, rows in store.tables.items()}
    assert len(tables["delegation_eval_item_verdicts"]) == len(_fixture()["items"])
    assert len(tables["delegation_eval_results"]) == len(first["results"])
    redelivered = _payload(at=datetime(2026, 9, 30, 0, 5, tzinfo=UTC)).model_dump(
        mode="json"
    )
    assert redelivered["eval_run_id"] == first["eval_run_id"]
    writer.handle(redelivered)
    assert {t: set(rows) for t, rows in store.tables.items()} == {
        t: set(rows) for t, rows in tables.items()
    }
    for table, rows in store.tables.items():
        for key, row in rows.items():
            kept = {k: v for k, v in row.items() if k != "observed_at"}
            assert kept == {
                k: v for k, v in tables[table][key].items() if k != "observed_at"
            }


def test_first_run_baseline_run_completed_row_count_is_never_zero() -> None:
    writer, _ = _writer()
    payload = _payload().model_dump(mode="json")
    first = writer.handle(payload)
    assert first["rows_upserted"] == first["verdict_rows"] + first["result_rows"] > 0
    duplicate = writer.handle(payload)
    assert duplicate["rows_upserted"] == 0
    assert duplicate["rows_refused_by_ordering_guard"] == first["rows_upserted"]
