# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness reading rules: what WORKING needs, and what makes a reading NOT WORKING."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessKind,
    EnumLabTestHarnessReadingState,
    EnumLabTestHarnessVerdict,
    ModelLabTestHarnessCaseOutcome,
    ModelLabTestHarnessFreshnessRequest,
    ModelLabTestHarnessReading,
    ModelLabTestHarnessResult,
    reading_at,
    reading_from_result,
)
from omnimarket.nodes.node_lab_test_harness_reading_compute.handlers import (
    HandlerLabTestHarnessReading,
)
from tests.helpers.lab_test_harness_fakes import T0, overlay_document

pytestmark = pytest.mark.unit

SHA = "a" * 40
MAX_AGE = 900


def _outcome(
    kind: EnumLabTestHarnessKind, verdict: EnumLabTestHarnessVerdict
) -> ModelLabTestHarnessCaseOutcome:
    required = (
        EnumLabTestHarnessVerdict.PASS
        if kind is EnumLabTestHarnessKind.GOLDEN
        else EnumLabTestHarnessVerdict.FAIL
    )
    return ModelLabTestHarnessCaseOutcome(
        case_id=f"{kind.value}-case",
        kind=kind,
        work_unit_id="lth-0123456789abcdef",
        host="h202",
        verdict=verdict,
        exit_code=0 if verdict is EnumLabTestHarnessVerdict.PASS else 3,
        as_expected=verdict is required,
    )


def _result(*outcomes: ModelLabTestHarnessCaseOutcome) -> ModelLabTestHarnessResult:
    return ModelLabTestHarnessResult(
        run_id="run-1",
        piece="central-test-harness",
        tag="drill",
        overlay_revision="r1",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=4),
        outcomes=outcomes,
    )


GOLDEN, ERROR = EnumLabTestHarnessKind.GOLDEN, EnumLabTestHarnessKind.ERROR
PASS, FAIL = EnumLabTestHarnessVerdict.PASS, EnumLabTestHarnessVerdict.FAIL


def _good() -> ModelLabTestHarnessReading:
    return reading_from_result(
        _result(_outcome(GOLDEN, PASS), _outcome(ERROR, FAIL)),
        max_age_seconds=MAX_AGE,
        now=T0 + timedelta(seconds=5),
    )


def test_golden_pass_and_error_red_is_working() -> None:
    reading = _good()
    assert reading.state is EnumLabTestHarnessReadingState.WORKING
    assert not reading.blocks_admission
    assert reading.probed_at == T0 + timedelta(seconds=4)


def test_a_broken_input_that_passes_is_not_working() -> None:
    reading = reading_from_result(
        _result(_outcome(GOLDEN, PASS), _outcome(ERROR, PASS)),
        max_age_seconds=MAX_AGE,
        now=T0,
    )
    assert reading.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert reading.blocks_admission
    assert "did not go red" in reading.reason


def test_a_golden_failure_and_a_missing_receipt_are_not_working() -> None:
    failed = reading_from_result(
        _result(_outcome(GOLDEN, FAIL), _outcome(ERROR, FAIL)),
        max_age_seconds=MAX_AGE,
        now=T0,
    )
    infra = reading_from_result(
        _result(
            _outcome(GOLDEN, PASS),
            _outcome(ERROR, EnumLabTestHarnessVerdict.INFRA_ERROR),
        ),
        max_age_seconds=MAX_AGE,
        now=T0,
    )
    assert failed.state is infra.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert "golden-case" in failed.reason
    assert "infra_error" in infra.reason


def test_a_stale_reading_is_not_working_and_a_fresh_one_is_unchanged() -> None:
    stored = _good()
    fresh = reading_at(
        stored,
        piece=stored.piece,
        tag=stored.tag,
        max_age_seconds=MAX_AGE,
        now=T0 + timedelta(seconds=MAX_AGE),
    )
    stale = reading_at(
        stored,
        piece=stored.piece,
        tag=stored.tag,
        max_age_seconds=MAX_AGE,
        now=T0 + timedelta(seconds=MAX_AGE + 10),
    )
    assert fresh.state is EnumLabTestHarnessReadingState.WORKING
    assert stale.state is EnumLabTestHarnessReadingState.NOT_WORKING
    assert stale.blocks_admission
    assert stale.reason.startswith("stale")
    assert stale.probed_at == stored.probed_at


def test_no_reading_is_unknown_and_blocks() -> None:
    reading = reading_at(
        None, piece="central-test-harness", tag="drill", max_age_seconds=MAX_AGE, now=T0
    )
    assert reading.state is EnumLabTestHarnessReadingState.UNKNOWN
    assert reading.blocks_admission


def test_a_future_dated_or_other_tag_row_is_not_working() -> None:
    stored = _good()
    early = reading_at(
        stored,
        piece=stored.piece,
        tag=stored.tag,
        max_age_seconds=MAX_AGE,
        now=T0 - timedelta(hours=1),
    )
    other = reading_at(
        stored, piece=stored.piece, tag="operational", max_age_seconds=MAX_AGE, now=T0
    )
    assert early.state is other.state is EnumLabTestHarnessReadingState.NOT_WORKING


def test_the_reading_node_gives_the_rule_reading() -> None:
    stored = _good()
    request = ModelLabTestHarnessFreshnessRequest(
        piece=stored.piece,
        tag=stored.tag,
        max_age_seconds=MAX_AGE,
        now=T0 + timedelta(days=1),
        stored=stored,
    )
    reading = HandlerLabTestHarnessReading().handle(request)
    assert reading.state is EnumLabTestHarnessReadingState.NOT_WORKING


def test_an_overlay_needs_a_golden_and_an_error_case_and_a_named_lane_for_kafka(
    tmp_path: object,
) -> None:
    from pathlib import Path

    from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay

    document = overlay_document(SHA, Path("/x/result.json"))
    cases = document["cases"]
    assert isinstance(cases, list)
    with pytest.raises(ValidationError, match="golden case and one error case"):
        ModelLabTestHarnessOverlay.model_validate(
            {**document, "cases": [cases[0], {**cases[0], "case_id": "second"}]}
        )
    with pytest.raises(ValidationError, match="needs bus_lane"):
        ModelLabTestHarnessOverlay.model_validate({**document, "backend": "kafka"})
    with pytest.raises(ValidationError):
        ModelLabTestHarnessOverlay.model_validate({**document, "extra_key": 1})


def test_the_result_path_reads_its_names_from_the_environment_and_refuses_a_missing_one() -> (
    None
):
    from pathlib import Path

    from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
        resolve_result_path,
    )

    assert resolve_result_path(
        "${STATE}/readings/x.json", {"STATE": "/var/state"}
    ) == Path("/var/state/readings/x.json")
    with pytest.raises(ValueError, match="does not supply"):
        resolve_result_path("${STATE}/readings/x.json", {})
