# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""validate_stage_receipt: the contract route, the golden chain and the error chain of the stage receipt operation."""

from __future__ import annotations

import datetime
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_in_memory import (
    SourceAttributionRecordsInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_receipt_validation import (
    HandlerStageReceiptValidation,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation,
    ModelReceiptNight,
    ModelStageReceipt,
    ModelStageReceiptValidationRequest,
    ModelStageReceiptValidationResult,
    ModelStageReceiptViolation,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_attribution_record_source import (
    ProtocolAttributionRecordSource,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_test_selection_selector_compute"
)
FIXTURES = REPO_ROOT / "tests" / "ci" / "fixtures" / "selection_stage"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _records(name: str) -> list[ModelPublishedAttributionRecord]:
    return [ModelPublishedAttributionRecord.model_validate(r) for r in _load(name)]


def _request(**changes: Any) -> ModelStageReceiptValidationRequest:
    payload: dict[str, Any] = {
        "current_stage": 1,
        "base_stage": 0,
        "receipts": [{"stage": 0}, _load("receipt_valid.json")],
    }
    payload.update(changes)
    return ModelStageReceiptValidationRequest.model_validate(payload)


def _handler(
    records: list[ModelPublishedAttributionRecord] | None,
) -> HandlerStageReceiptValidation:
    return HandlerStageReceiptValidation(
        SourceAttributionRecordsInMemory(records), load_policy()
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


def test_golden_chain_a_valid_stage_one_receipt_is_accepted() -> None:
    result = _handler(_records("records_valid.json")).handle(_request())
    assert result == ModelStageReceiptValidationResult(valid=True, violations=())


def test_golden_chain_the_baseline_alone_is_accepted_at_stage_zero() -> None:
    result = _handler(None).handle(
        _request(current_stage=0, base_stage=None, receipts=[{"stage": 0}])
    )
    assert result.valid


def test_error_chain_each_refusal_carries_its_code_and_stage() -> None:
    result = _handler(_records("records_valid.json")).handle(
        _request(receipts=[{"stage": 0}])
    )
    assert result.violations == (
        ModelStageReceiptViolation(
            code=EnumStageReceiptViolation.RECEIPT_MISSING,
            stage=1,
            detail="selection_stage is 1 and no receipt names stage 1",
        ),
    )


def test_error_chain_two_receipts_for_one_stage_are_refused() -> None:
    receipts = [{"stage": 0}, {"stage": 0}, _load("receipt_valid.json")]
    result = _handler(_records("records_valid.json")).handle(
        _request(receipts=receipts)
    )
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.DUPLICATE_RECEIPT
    ]


def test_error_chain_a_receipt_with_no_identity_is_refused_before_any_record_is_read() -> (
    None
):
    raw = _load("receipt_valid.json")
    for key in ("repository", "selector_version", "policy_version"):
        raw.pop(key)
    result = _handler(None).handle(_request(receipts=[{"stage": 0}, raw]))
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.RECEIPT_UNIDENTIFIED
    ]


def test_error_chain_nights_that_skip_a_day_are_not_consecutive() -> None:
    records = _records("records_valid.json")
    gapped = [
        r.model_copy(
            update={
                "nightly_slot_date": r.nightly_slot_date
                + datetime.timedelta(days=i % 2)
            }
        )
        for i, r in enumerate(records)
    ]
    raw = _load("receipt_valid.json")
    raw["nights"] = [
        {
            "slot_date": r.nightly_slot_date.isoformat(),
            "nightly_run_id": r.nightly_run_id,
            "tested_head_sha": r.tested_head_sha,
        }
        for r in gapped
    ]
    result = _handler(gapped).handle(_request(receipts=[{"stage": 0}, raw]))
    assert EnumStageReceiptViolation.NIGHTS_NOT_CONSECUTIVE in {
        v.code for v in result.violations
    }


def test_error_chain_a_slot_published_for_another_date_is_refused() -> None:
    records = _records("records_valid.json")
    moved = [
        records[0].model_copy(update={"nightly_slot_date": datetime.date(2027, 1, 1)})
    ]
    result = _handler([*moved, *records[1:]]).handle(_request())
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.RECORD_MISMATCH
    ]


def test_error_chain_a_night_with_no_pull_request_record_is_not_clean() -> None:
    records = _records("records_valid.json")
    empty = [
        records[0].model_copy(update={"pull_request_record_count": 0}),
        *records[1:],
    ]
    raw = _load("receipt_valid.json")
    raw["pr_record_count"] = 12
    raw["clean_nights"] = 6
    raw["clean_night_share"] = 6 / 7
    result = _handler(empty).handle(_request(receipts=[{"stage": 0}, raw]))
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.NIGHT_NOT_CLEAN
    ]


def test_error_chain_an_unattributable_or_incomplete_night_is_not_clean() -> None:
    for field in ("unattributable_count", "incomplete_count", "miss_count"):
        records = _records("records_valid.json")
        dirty = [records[2].model_copy(update={field: 1})]
        result = _handler([*records[:2], *dirty, *records[3:]]).handle(_request())
        assert EnumStageReceiptViolation.NIGHT_NOT_CLEAN in {
            v.code for v in result.violations
        }, field


def test_error_chain_too_few_pull_request_records_is_refused() -> None:
    policy = load_policy().model_copy(update={"stage_min_pr_records": 15})
    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsInMemory(_records("records_valid.json")), policy
    )
    result = handler.handle(_request())
    assert [v.code for v in result.violations] == [
        EnumStageReceiptViolation.TOO_FEW_PR_RECORDS
    ]


# -- models ---------------------------------------------------------------------


def test_published_record_holds_exactly_the_public_safe_fields() -> None:
    assert list(ModelPublishedAttributionRecord.model_fields) == [
        "repository",
        "nightly_slot_date",
        "nightly_run_id",
        "tested_head_sha",
        "selector_version",
        "policy_version",
        "stage_entry_counted",
        "pull_request_record_count",
        "miss_count",
        "interaction_count",
        "preexisting_count",
        "unattributable_count",
        "incomplete_count",
        "status",
    ]


def test_published_record_status_must_agree_with_the_counts() -> None:
    payload = _records("records_valid.json")[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate(
            {**payload, "status": "incomplete"}
        )
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate(
            {**payload, "incomplete_count": 1}
        )
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate(
            {**payload, "status": "incomplete", "incomplete_count": 1}
        )
    incomplete = ModelPublishedAttributionRecord.model_validate(
        {
            **payload,
            "status": "incomplete",
            "incomplete_count": 1,
            "interaction_count": 0,
        }
    )
    assert incomplete.status.value == "incomplete"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "no-owner-part"),
        ("tested_head_sha", "abc"),
        ("selector_version", "has space"),
        ("policy_version", ""),
    ],
)
def test_published_record_refuses_values_outside_the_producers_patterns(
    field: str, value: str
) -> None:
    payload = _records("records_valid.json")[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate({**payload, field: value})


def test_error_chain_an_incomplete_night_is_never_a_clean_night() -> None:
    records = _records("records_valid.json")
    payload = {
        **records[2].model_dump(mode="json"),
        "status": "incomplete",
        "incomplete_count": 1,
        "interaction_count": 0,
    }
    incomplete = ModelPublishedAttributionRecord.model_validate(payload)
    result = _handler([*records[:2], incomplete, *records[3:]]).handle(_request())
    assert EnumStageReceiptViolation.NIGHT_NOT_CLEAN in {
        v.code for v in result.violations
    }


def test_published_record_is_frozen_and_refuses_an_extra_or_negative_field() -> None:
    record = _records("records_valid.json")[0]
    with pytest.raises(ValidationError):
        record.miss_count = 3  # type: ignore[misc]
    payload = record.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate({**payload, "owner": "someone"})
    with pytest.raises(ValidationError):
        ModelPublishedAttributionRecord.model_validate({**payload, "miss_count": -1})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "stage": 0,
            "nights": [
                {
                    "slot_date": "2026-12-01",
                    "nightly_run_id": 1,
                    "tested_head_sha": "a" * 40,
                }
            ],
        },
        {"stage": 0, "entered_from_stage": 0},
        {"stage": 1},
        {"stage": 5, "entered_from_stage": 4},
        {"stage": 1, "entered_from_stage": 0, "unexpected": True},
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


# -- the in-memory twin of the record source ------------------------------------------


def _nights(records: list[ModelPublishedAttributionRecord]) -> list[ModelReceiptNight]:
    return [
        ModelReceiptNight(
            slot_date=r.nightly_slot_date,
            nightly_run_id=r.nightly_run_id,
            tested_head_sha=r.tested_head_sha,
        )
        for r in records
    ]


def test_in_memory_source_answers_only_the_asked_runs_of_the_asked_repository() -> None:
    records = _records("records_valid.json")
    source: ProtocolAttributionRecordSource = SourceAttributionRecordsInMemory(records)
    got = source.read_records(records[0].repository, _nights(records[:1]))
    assert got is not None
    assert got.records == (records[0],)
    other = source.read_records("OmniNode-ai/other", _nights(records[:1]))
    assert other is not None
    assert other.records == ()


def test_in_memory_source_built_without_records_is_unreadable() -> None:
    assert (
        SourceAttributionRecordsInMemory(None).read_records(
            "OmniNode-ai/omnimarket", _nights(_records("records_valid.json")[:1])
        )
        is None
    )
