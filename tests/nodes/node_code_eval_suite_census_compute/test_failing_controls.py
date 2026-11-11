# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failing controls for node_code_eval_suite_census_compute.

A task whose control is fail_check is never in candidates and not in any
admitted count; an unscorable control with source falsifier_not_held counts
under does_not_hold and one with source harness counts under could_not_run;
shortfall is max(0, required - admitted) and is 0 when admitted exceeds it;
a band with no tasks still has a row with admitted 0; pre_control_exclusions
with equal reasons are summed under pre_control:<reason>; and the task model
refuses control pass with a source, unscorable without one, and fail_check
with a source.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.code_eval_suite.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
    ModelCodeEvalSuiteExclusion,
)
from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalSuiteCensus,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from tests.nodes.node_code_eval_suite_census_compute.support import (
    overlay,
    task,
)

pytestmark = pytest.mark.unit


def _handle(request: ModelCodeEvalSuiteCensusRequest) -> ModelCodeEvalSuiteCensus:
    return HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(request)


def test_fail_check_task_is_never_in_candidates_or_admitted_counts() -> None:
    t = task(control=EnumCodeScoreVerdict.FAIL_CHECK)
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(t,)))
    assert answer.failure is None
    assert answer.candidates == ()
    assert answer.control is not None
    assert answer.control.holds == 0
    assert answer.control.does_not_hold == 1
    assert answer.control.could_not_run == 0
    assert all(row.admitted == 0 for row in answer.by_band)
    assert all(row.admitted == 0 for row in answer.by_repository)
    assert all(row.admitted == 0 for row in answer.by_band_repository)
    assert ("control:fail_check", 1) in [(r.reason, r.count) for r in answer.excluded]


def test_unscorable_falsifier_not_held_counts_under_does_not_hold() -> None:
    t = task(
        control=EnumCodeScoreVerdict.UNSCORABLE,
        control_source=EnumCodeScoreSource.FALSIFIER_NOT_HELD,
    )
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(t,)))
    assert answer.control is not None
    assert answer.control.does_not_hold == 1
    assert answer.control.could_not_run == 0
    assert ("control:unscorable:falsifier_not_held", 1) in [
        (r.reason, r.count) for r in answer.excluded
    ]


def test_unscorable_harness_counts_under_could_not_run() -> None:
    t = task(
        control=EnumCodeScoreVerdict.UNSCORABLE,
        control_source=EnumCodeScoreSource.HARNESS,
    )
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(t,)))
    assert answer.control is not None
    assert answer.control.could_not_run == 1
    assert answer.control.does_not_hold == 0
    assert ("control:unscorable:harness", 1) in [
        (r.reason, r.count) for r in answer.excluded
    ]


def test_shortfall_is_zero_when_admitted_exceeds_required() -> None:
    required = (
        _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(task(),)))
        .by_band[0]
        .required_cell_n
    )
    tasks = tuple(task() for _ in range(required + 2))
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=tasks))
    assert answer.control is not None
    assert answer.control.holds == required + 2
    by_band = {row.band: row for row in answer.by_band}
    assert by_band[EnumSizeBand.S].admitted == required + 2
    assert by_band[EnumSizeBand.S].cell_shortfall == 0
    assert by_band[EnumSizeBand.M].cell_shortfall == required
    assert by_band[EnumSizeBand.L].cell_shortfall == required


def test_shortfall_is_max_of_zero_and_required_minus_admitted() -> None:
    t = task(control=EnumCodeScoreVerdict.PASS)
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(t,)))
    assert answer.control is not None
    assert answer.control.holds == 1
    for row in answer.by_band:
        assert row.cell_shortfall == max(0, row.required_cell_n - row.admitted)
        assert row.paired_shortfall == max(0, row.required_paired_n - row.admitted)
    assert sum(row.admitted for row in answer.by_band) == 1


def test_band_with_no_tasks_still_has_a_row_with_admitted_zero() -> None:
    t = task(control=EnumCodeScoreVerdict.PASS)
    answer = _handle(ModelCodeEvalSuiteCensusRequest(suite_id="s", tasks=(t,)))
    assert len(answer.by_band) == 3
    for row in answer.by_band:
        assert row.admitted >= 0
    assert any(row.admitted == 0 for row in answer.by_band)


def test_pre_control_exclusions_with_equal_reasons_are_summed() -> None:
    t = task(control=EnumCodeScoreVerdict.PASS)
    answer = _handle(
        ModelCodeEvalSuiteCensusRequest(
            suite_id="s",
            tasks=(t,),
            pre_control_exclusions=(
                ModelCodeEvalSuiteExclusion(reason="duplicate", count=2),
                ModelCodeEvalSuiteExclusion(reason="duplicate", count=3),
            ),
        )
    )
    assert ("pre_control:duplicate", 5) in [
        (r.reason, r.count) for r in answer.excluded
    ]


def test_task_model_refuses_pass_with_a_source() -> None:
    with pytest.raises(ValidationError):
        task(
            control=EnumCodeScoreVerdict.PASS,
            control_source=EnumCodeScoreSource.CONTRACT,
        )


def test_task_model_refuses_unscorable_without_a_source() -> None:
    with pytest.raises(ValidationError):
        task(control=EnumCodeScoreVerdict.UNSCORABLE, control_source=None)


def test_task_model_refuses_fail_check_with_a_source() -> None:
    with pytest.raises(ValidationError):
        task(
            control=EnumCodeScoreVerdict.FAIL_CHECK,
            control_source=EnumCodeScoreSource.CONTRACT,
        )
