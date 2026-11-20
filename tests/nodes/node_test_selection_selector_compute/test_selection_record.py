# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""build_selection_record: the typed record of one pull request's test selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import (
    POLICY_PATH,
    load_policy,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_record import (
    HandlerSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    ModelSelectionRecordRequest,
    ModelTestSelection,
    ModelTestSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_test_selection_selector_compute"
)
DURATIONS = {"tests/a/test_x.py": 250.0, "tests/b/test_y.py": 90.0}


def _policy(stage: int = 0) -> ModelSelectorPolicy:
    return load_policy().model_copy(update={"selection_stage": stage})


def _handler(stage: int = 0) -> HandlerSelectionRecord:
    policy = _policy(stage)
    return HandlerSelectionRecord(
        HandlerTestSelectionSelector(SourceInMemory(DURATIONS), policy), policy
    )


def _narrowed(path: str, shards: int) -> ModelTestSelection:
    return ModelTestSelection(
        selected_paths=[path],
        split_count=shards,
        is_full_suite=False,
        matrix=list(range(1, shards + 1)),
    )


def _request(
    stages: dict[int, ModelTestSelection] | None = None, *, flag: bool = True
) -> ModelSelectionRecordRequest:
    return ModelSelectionRecordRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=42,
        head_sha="1" * 40,
        base_sha="2" * 40,
        checkout_sha="3" * 40,
        run_id=9001,
        run_attempt=2,
        changed_files=("src/omnimarket/models/x.py", "tests/a/test_x.py"),
        flag_enabled=flag,
        stage_selections=stages or {0: _narrowed("tests/a/", 1)},
        repo_root="/unused",
        selector_version="1.0.0",
        policy_version="abcdef012345",
    )


def test_golden_record_holds_every_field_the_plan_lists() -> None:
    record = _handler().handle(_request())
    assert isinstance(record, ModelTestSelectionRecord)
    assert record.repo == "OmniNode-ai/omnimarket"
    assert record.pr_number == 42
    assert record.head_sha == "1" * 40
    assert record.base_sha == "2" * 40
    assert record.checkout_sha == "3" * 40
    assert record.run_id == 9001
    assert record.run_attempt == 2
    assert record.changed_files == (
        "src/omnimarket/models/x.py",
        "tests/a/test_x.py",
    )
    assert record.policy_version == "abcdef012345"
    assert record.selection_stage == 0
    assert record.graph_source == "none"
    executed = record.executed_selection
    assert executed.kind == "narrowed"
    assert executed.reason_code is None
    assert executed.selected_paths == ["tests/a/"]
    assert executed.split_count == 1
    assert executed.selected_seconds == 250.0
    assert executed.selector_version == "1.0.0"
    assert executed.policy_version == "abcdef012345"
    assert record.selection_by_stage == {0: executed}


def test_stage_gate_runs_the_entry_of_the_policy_stage_and_no_other() -> None:
    stages = {
        0: _narrowed("tests/a/", 1),
        1: _narrowed("tests/b/", 1),
        2: _narrowed("tests/a/", 2),
    }
    record = _handler(stage=1).handle(_request(stages))
    assert record.selection_stage == 1
    assert record.executed_selection.selected_paths == ["tests/b/"]
    assert sorted(record.selection_by_stage) == [0, 1, 2]
    assert record.selection_by_stage[2].split_count == 2
    stage_zero = _handler(stage=0).handle(_request(stages))
    assert stage_zero.executed_selection.selected_paths == ["tests/a/"]


def test_flag_off_executes_the_full_matrix_and_leaves_the_entries() -> None:
    stages = {0: _narrowed("tests/a/", 1)}
    on = _handler().handle(_request(stages, flag=True))
    off = _handler().handle(_request(stages, flag=False))
    full_split_count = load_policy().full_split_count
    assert off.executed_selection.kind == "full_suite"
    assert off.executed_selection.reason_code is EnumFullSuiteReason.FEATURE_FLAG_OFF
    assert off.executed_selection.split_count == full_split_count
    assert off.executed_selection.selected_seconds == 340.0
    assert off.selection_by_stage == on.selection_by_stage


def test_error_a_missing_entry_for_the_policy_stage_escalates_to_full() -> None:
    record = _handler(stage=2).handle(_request({0: _narrowed("tests/a/", 1)}))
    assert record.executed_selection.kind == "full_suite"
    assert (
        record.executed_selection.reason_code
        is EnumFullSuiteReason.SELECTION_UNAVAILABLE
    )
    assert record.executed_selection.split_count == load_policy().full_split_count


def test_error_requests_with_a_gap_in_the_stages_or_no_stage_are_refused() -> None:
    with pytest.raises(ValidationError):
        _request({1: _narrowed("tests/a/", 1)})
    with pytest.raises(ValidationError):
        ModelSelectionRecordRequest.model_validate(
            {**_request().model_dump(), "stage_selections": {}}
        )
    with pytest.raises(ValidationError):
        ModelSelectionRecordRequest.model_validate(
            {**_request().model_dump(), "surplus": 1}
        )


def test_record_is_frozen_and_forbids_extra_keys() -> None:
    record = _handler().handle(_request())
    with pytest.raises(ValidationError):
        record.repo = "other"  # type: ignore[misc]
    payload = json.loads(record.model_dump_json())
    with pytest.raises(ValidationError):
        ModelTestSelectionRecord.model_validate({**payload, "surplus": 1})


def _payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        _handler().handle(_request()).model_dump_json()
    )
    return payload


def test_record_round_trips_through_json() -> None:
    record = _handler().handle(_request())
    assert (
        ModelTestSelectionRecord.model_validate_json(record.model_dump_json()) == record
    )


@pytest.mark.parametrize(
    "key",
    [
        "repo",
        "pr_number",
        "head_sha",
        "base_sha",
        "checkout_sha",
        "run_id",
        "run_attempt",
        "changed_files",
        "policy_version",
        "selection_stage",
        "executed_selection",
        "selection_by_stage",
        "graph_source",
    ],
)
def test_record_without_a_top_level_key_is_refused(key: str) -> None:
    payload = _payload()
    del payload[key]
    with pytest.raises(ValidationError):
        ModelTestSelectionRecord.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "key",
    [
        "kind",
        "reason_code",
        "selected_paths",
        "split_count",
        "selected_seconds",
        "selector_version",
        "policy_version",
    ],
)
def test_record_stage_entry_without_a_key_is_refused(key: str) -> None:
    for where in ("executed", "stage"):
        payload = _payload()
        entry = (
            payload["executed_selection"]
            if where == "executed"
            else payload["selection_by_stage"]["0"]
        )
        del entry[key]
        with pytest.raises(ValidationError):
            ModelTestSelectionRecord.model_validate_json(json.dumps(payload))


def test_policy_holds_the_stage_gate_and_the_record_settings() -> None:
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert raw["selection_stage"] == 0
    assert raw["full_split_count"] == 20
    assert raw["record_retention_days"] == 14
    policy = load_policy()
    assert (
        policy.selection_stage,
        policy.full_split_count,
        policy.record_retention_days,
    ) == (0, 20, 14)


def test_contract_routes_build_selection_record_to_its_handler() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    routes = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    route = routes["build_selection_record"]
    assert route["handler"]["name"] == "HandlerSelectionRecord"
    assert route["handler"]["module"].endswith("handlers.handler_selection_record")
    assert route["input_model"].endswith(".ModelSelectionRecordRequest")
    assert route["output_model"].endswith(".ModelTestSelectionRecord")
