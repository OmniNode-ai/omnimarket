# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_design_record_check_compute: R9 refusals, exit_code, severity config, bindings (OMN-21460)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_design_record_check_compute import (
    NodeDesignRecordCheckCompute,
)
from omnimarket.nodes.node_design_record_check_compute.handlers.handler_design_record_check import (
    CONTRACT_PATH,
    HandlerDesignRecordCheck,
)
from omnimarket.nodes.node_design_record_check_compute.models.model_design_record_check import (
    EnumDesignRecordIssue,
    ModelDesignRecordCheckRequest,
    ModelDesignRecordCheckResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"

HEAD = """\
Intended state: the queue drains within one hour.
Live check command: onex run-node node_queue_probe
Live check failing reading: depth 40 before the build.
"""
HEADER = "| Failure mode | How | Detected by | Severity | Detector |\n| -- | -- | -- | -- | -- |\n"


def _check(
    text: str, handler: HandlerDesignRecordCheck | None = None
) -> ModelDesignRecordCheckResult:
    node = handler or HandlerDesignRecordCheck()
    return node.handle(ModelDesignRecordCheckRequest(text=text, now=NOW))


def _codes(result: ModelDesignRecordCheckResult) -> set[EnumDesignRecordIssue]:
    return {issue.code for issue in result.issues}


def test_empty_failure_mode_table_is_refused_naming_the_part() -> None:
    result = _check(HEAD + HEADER)
    assert EnumDesignRecordIssue.FAILURE_MODE_TABLE_EMPTY in _codes(result)
    assert any(i.part == "failure-mode table" for i in result.issues)
    assert result.failure_mode_rows == 0


def test_missing_table_is_refused() -> None:
    assert EnumDesignRecordIssue.FAILURE_MODE_TABLE_MISSING in _codes(_check(HEAD))


def test_row_with_no_detector_and_no_gap_is_refused() -> None:
    result = _check(HEAD + HEADER + "| stalls | lock | none | 2 | |\n")
    assert _codes(result) == {EnumDesignRecordIssue.ROW_WITHOUT_DETECTOR_OR_GAP}
    assert result.rows_without_detector_or_gap == 1
    assert result.issues[0].row == 1


def test_row_with_gap_mark_or_detector_is_accepted() -> None:
    body = (
        HEAD
        + HEADER
        + "| stalls | lock | none | 2 | GAP |\n| dup | race | probe | 1 | test_x.py |\n"
    )
    result = _check(body)
    assert result.issues == ()
    assert result.failure_mode_rows == 2
    assert result.rows_without_detector_or_gap == 0


def test_no_live_check_command_is_refused() -> None:
    body = HEAD.replace("Live check command: onex run-node node_queue_probe\n", "")
    result = _check(body + HEADER + "| a | b | c | 1 | GAP |\n")
    assert _codes(result) == {EnumDesignRecordIssue.LIVE_CHECK_COMMAND_MISSING}
    assert result.live_check_command_present is False


def test_body_without_a_record_is_refused() -> None:
    result = _check("# A plan\n\nNothing here.\n")
    assert _codes(result) == {EnumDesignRecordIssue.RECORD_MISSING}


def test_section_11_3_fixture_passes() -> None:
    result = _check((FIXTURES / "section_11_3_design_record.md").read_text("utf-8"))
    assert result.issues == ()
    assert result.exit_code == 0
    assert result.failure_mode_rows == 3


def test_any_issue_sets_exit_code_nonzero_and_none_sets_zero() -> None:
    good = HEAD + HEADER + "| a | b | c | 1 | GAP |\n"
    assert _check(good).exit_code == 0
    assert _check(HEAD + HEADER).exit_code != 0
    assert _check("nothing").exit_code != 0


def _contract_with_levels(tmp_path: Path, levels: list[int]) -> Path:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    contract["config"]["design_process"]["severity_levels"] = levels
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def test_committed_levels_are_one_to_three() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    assert contract["config"]["design_process"]["severity_levels"] == [1, 2, 3]


def test_severity_four_refused_under_committed_and_accepted_under_fixture(
    tmp_path: Path,
) -> None:
    body = HEAD + HEADER + "| a | b | c | 4 | probe |\n"
    refused = _check(body)
    assert _codes(refused) == {EnumDesignRecordIssue.SEVERITY_OUT_OF_RANGE}
    fixture = HandlerDesignRecordCheck(_contract_with_levels(tmp_path, [1, 2, 3, 4]))
    assert _check(body, fixture).issues == ()


def test_no_protocol_dependencies_in_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    assert "protocol_dependencies" not in contract


def test_in_process_and_entry_wrapper_are_byte_identical() -> None:
    request = ModelDesignRecordCheckRequest(
        text=(FIXTURES / "section_11_3_design_record.md").read_text("utf-8"), now=NOW
    )
    direct = HandlerDesignRecordCheck().handle(request).model_dump_json()
    wrapped = NodeDesignRecordCheckCompute().handle(request).model_dump_json()
    assert direct == wrapped


def test_terminal_event_is_declared_in_the_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    assert contract["terminal_event"] == "onex.evt.omnimarket.design-record-checked.v1"
