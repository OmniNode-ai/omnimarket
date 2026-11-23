# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""validate_stage_receipt: the contract route, the golden chain and the error chain of the stage receipt operation."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_file import (
    SourceRecordedSetFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_in_memory import (
    SourceRecordedSetInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_receipt_validation import (
    HandlerStageReceiptValidation,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation,
    ModelStageReceipt,
    ModelStageReceiptValidationRequest,
    ModelStageReceiptValidationResult,
    ModelStageReceiptViolation,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_recorded_set_source import (
    ProtocolRecordedSetSource,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_test_selection_selector_compute"
)
RECEIPT = REPO_ROOT / "config" / "test_selection_stage_receipts" / "stage_1.yaml"


def _stage_one() -> dict[str, Any]:
    raw = yaml.safe_load(RECEIPT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _recorded() -> ModelRecordedSet:
    recorded = SourceRecordedSetFile(REPO_ROOT).read_recorded_set(
        "config/test_selection_replay_input.json",
        "config/test_selection_observed_input.json",
    )
    assert recorded is not None
    return recorded


def _request(**changes: Any) -> ModelStageReceiptValidationRequest:
    payload: dict[str, Any] = {
        "current_stage": 1,
        "base_stage": 0,
        "receipts": [{"stage": 0}, _stage_one()],
    }
    payload.update(changes)
    return ModelStageReceiptValidationRequest.model_validate(payload)


def _handler(recorded: ModelRecordedSet | None) -> HandlerStageReceiptValidation:
    return HandlerStageReceiptValidation(
        SourceRecordedSetInMemory(recorded), load_policy()
    )


def test_contract_routes_validate_stage_receipt_to_its_handler() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    routes = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    route = routes["validate_stage_receipt"]
    assert route["handler"]["name"] == "HandlerStageReceiptValidation"
    module = importlib.import_module(route["handler"]["module"])
    assert hasattr(module, route["handler"]["name"])
    assert route["input_model"].endswith(".ModelStageReceiptValidationRequest")
    assert route["output_model"].endswith(".ModelStageReceiptValidationResult")


def test_golden_chain_the_stage_one_receipt_is_accepted() -> None:
    result = _handler(_recorded()).handle(_request())
    assert result == ModelStageReceiptValidationResult(valid=True, violations=())


def test_golden_chain_the_baseline_alone_is_accepted_at_stage_zero() -> None:
    result = _handler(None).handle(
        _request(current_stage=0, base_stage=None, receipts=[{"stage": 0}])
    )
    assert result.valid


def test_error_chain_each_refusal_carries_its_code_and_stage() -> None:
    result = _handler(_recorded()).handle(_request(receipts=[{"stage": 0}]))
    assert result.violations == (
        ModelStageReceiptViolation(
            code=EnumStageReceiptViolation.RECEIPT_MISSING,
            stage=1,
            detail="selection_stage is 1 and no receipt names stage 1",
        ),
    )


def test_error_chain_two_receipts_for_one_stage_are_refused() -> None:
    receipts = [{"stage": 0}, {"stage": 0}, _stage_one()]
    result = _handler(_recorded()).handle(_request(receipts=receipts))
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.DUPLICATE_RECEIPT
    ]


def test_error_chain_a_receipt_with_no_identity_is_refused_before_the_set_is_read() -> (
    None
):
    raw = _stage_one()
    for key in ("repository", "selector_version"):
        raw.pop(key)
    result = _handler(None).handle(_request(receipts=[{"stage": 0}, raw]))
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.RECEIPT_UNIDENTIFIED
    ]


def test_error_chain_a_set_that_cannot_be_read_is_never_a_pass() -> None:
    result = _handler(None).handle(_request())
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.RECORDED_SET_UNAVAILABLE
    ]


def test_error_chain_a_receipt_entering_from_the_wrong_stage_is_a_jump() -> None:
    raw = {**_stage_one(), "entered_from_stage": 0, "stage": 2}
    result = _handler(_recorded()).handle(
        _request(current_stage=2, base_stage=1, receipts=[{"stage": 0}, raw])
    )
    codes = {v.code for v in result.violations}
    assert EnumStageReceiptViolation.STAGE_JUMP in codes
    assert EnumStageReceiptViolation.RECEIPT_MISSING in codes


def test_error_chain_a_replay_that_fails_is_refused() -> None:
    recorded = _recorded()
    broken = recorded.model_copy(
        update={
            "replay_input": recorded.replay_input.model_copy(
                update={"tree_id": "0" * 40}
            )
        }
    )
    result = _handler(broken).handle(_request())
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.RECORDED_SET_UNAVAILABLE
    ]
    assert "the replay failed" in result.violations[0].detail


def test_error_chain_a_policy_asking_for_more_changes_than_were_compared_fails() -> (
    None
):
    policy = load_policy().model_copy(update={"stage_min_compared_changes": 61})
    handler = HandlerStageReceiptValidation(
        SourceRecordedSetInMemory(_recorded()), policy
    )
    result = handler.handle(_request())
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.COMPARISON_FAILED
    ]
    assert "60 changes compared; the policy asks for at least 61" in (
        result.violations[0].detail
    )


# -- models ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": 0, "comparison": _stage_one()["comparison"]},
        {"stage": 0, "entered_from_stage": 0},
        {"stage": 1},
        {"stage": 5, "entered_from_stage": 4},
        {"stage": 1, "entered_from_stage": 0, "unexpected": True},
        {"stage": 1, "entered_from_stage": 0, "nights": []},
    ],
)
def test_error_chain_a_malformed_receipt_is_a_typed_validation_error(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelStageReceipt.model_validate(payload)


def test_receipt_and_result_are_frozen() -> None:
    receipt = ModelStageReceipt(stage=0)
    with pytest.raises(ValidationError):
        receipt.stage = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ModelStageReceiptValidationResult(
            valid=True,
            violations=(
                ModelStageReceiptViolation(
                    code=EnumStageReceiptViolation.RECEIPT_MISSING, stage=1, detail="x"
                ),
            ),
        )


# -- the sources of the recorded set --------------------------------------------------


def test_in_memory_source_answers_its_set_for_any_paths() -> None:
    recorded = _recorded()
    source: ProtocolRecordedSetSource = SourceRecordedSetInMemory(recorded)
    assert source.read_recorded_set("a", "b") == recorded


def test_in_memory_source_built_without_a_set_is_unreadable() -> None:
    assert SourceRecordedSetInMemory(None).read_recorded_set("a", "b") is None


def test_file_source_reads_the_recorded_runs_and_their_digests() -> None:
    recorded = _recorded()
    assert len(recorded.runs) == 60
    assert sum(run.readable for run in recorded.runs) == 17
    assert all(run.conclusion == "success" for run in recorded.runs if run.readable)
    assert len(recorded.replay_input.merges) == 60


def test_file_source_cannot_read_a_missing_or_malformed_file(tmp_path: Path) -> None:
    source = SourceRecordedSetFile(tmp_path)
    assert source.read_recorded_set("nope.json", "nope.json") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert source.read_recorded_set("bad.json", "bad.json") is None
