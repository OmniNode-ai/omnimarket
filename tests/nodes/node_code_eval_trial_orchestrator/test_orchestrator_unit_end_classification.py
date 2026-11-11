# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC12 through the orchestrator handler: each row of rule 2's table (how the
unit ended) gives its verdict, an end E2 never scores is never sent to it, and the verdict
reaches the published row. Exemplar: ``test_unit_end_classification.py`` (the reducer's own)."""

from __future__ import annotations

import pytest

from omnimarket.models.model_delegated_code_edit import ModelCodeEditResult
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    trial_columns,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import ModelVerdictRow
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    completed_host,
    make_bus,
    run_request,
    unit_failure,
)

pytestmark = pytest.mark.unit


def result(status: str) -> ModelCodeEditResult:
    return ModelCodeEditResult(loop_run_id="loop-1", status=status, turns=1)


# name, receipt fields, first verdict label, sent to E2
CASES = [
    ("refused", {"status": "refused"}, "unscorable(harness)", False),
    ("infra_error", {"status": "infra_error"}, "unscorable(harness)", False),
    (
        "diff_truncated",
        {"diff_truncated": True},
        "unscorable(draft_not_recoverable)",
        False,
    ),
    ("exit_1", {"exit_code": 1}, "unscorable(harness)", False),
    ("no_result", {"code_edit_result": None}, "unscorable(harness)", False),
    (
        "loop_infra_error",
        {"code_edit_result": result("infra_error")},
        "unscorable(harness)",
        False,
    ),
    (
        "loop_delegate_failed",
        {"code_edit_result": result("delegate_failed")},
        "unscorable(harness)",
        False,
    ),
    ("accepted", {}, "fail_check", True),
    (
        "checks_failed",
        {"code_edit_result": result("checks_failed")},
        "fail_check",
        True,
    ),
    (
        "timed_out",
        {"status": "timed_out", "exit_code": None, "code_edit_result": None},
        "fail_check",
        True,
    ),
]


@pytest.mark.parametrize(("name", "fields", "label", "sent"), CASES)
def test_each_row_of_rule_2_gives_its_verdict(name, fields, label, sent) -> None:
    bus = make_bus(host=lambda unit: completed_host(unit, **fields))
    bus.send(run_request(1, 1))
    bus.drain()
    try_scores = [s for s in bus.scores if s.try_identity.model_arm]
    assert bool(try_scores) is sent, name
    sequence = bus.closes[0].sequences[0]
    assert sequence.verdicts[0] == label, name
    # the bound of 3: a try that does not pass is followed by the next one
    assert len(sequence.verdicts) == 3, name
    if sent:
        # a score request for a timed-out unit carries the diff the receipt returned
        assert all(s.draft_diff for s in try_scores) or name == "timed_out_empty"


def test_a_timed_out_unit_with_a_diff_is_scored_with_timed_out_with_diff_set() -> None:
    bus = make_bus(
        host=lambda unit: completed_host(
            unit, status="timed_out", exit_code=None, code_edit_result=None
        )
    )
    bus.send(run_request(1, 1))
    bus.drain()
    tries = [s for s in bus.scores if s.try_identity.model_arm]
    assert tries
    assert all(s.try_identity.timed_out_with_diff for s in tries)


def test_a_timed_out_unit_with_no_diff_is_scored_as_an_empty_draft() -> None:
    bus = make_bus(
        host=lambda unit: completed_host(
            unit, status="timed_out", exit_code=None, code_edit_result=None, diff=""
        )
    )
    bus.send(run_request(1, 1))
    bus.drain()
    tries = [s for s in bus.scores if s.try_identity.model_arm]
    assert tries
    assert all(
        s.draft_diff == "" and not s.try_identity.timed_out_with_diff for s in tries
    )


def test_a_failure_terminal_matched_by_the_runs_correlation_id_is_unscorable_harness() -> (
    None
):
    bus = make_bus(host=unit_failure)
    bus.send(run_request(1, 1))
    bus.drain()
    assert not [s for s in bus.scores if s.try_identity.model_arm]
    assert bus.closes[0].sequences[0].verdicts == ("unscorable(harness)",) * 3


def test_an_empty_correlation_id_on_the_failure_falls_back_to_the_lane() -> None:
    def failure(unit: ModelLabWorkUnitRequest):
        return unit_failure(unit).model_copy(update={"correlation_id": ""})

    bus = make_bus(host=failure)
    bus.send(run_request(1, 1))
    bus.drain()
    assert bus.closes[0].sequences[0].verdicts == ("unscorable(harness)",) * 3


@pytest.mark.parametrize(
    ("verdict", "source", "kind", "ordinal"),
    [
        (EnumCodeScoreVerdict.PASS, None, "control", 1),
        (EnumCodeScoreVerdict.FAIL_CHECK, None, "try", 2),
    ],
)
def test_the_row_columns_read_the_verdict_beside_the_try(
    verdict, source, kind, ordinal
) -> None:
    row = ModelVerdictRow(
        task_key="t",
        model_arm="" if kind == "control" else "m",
        try_ordinal=ordinal,
        is_control=kind == "control",
        verdict=verdict,
        source=source,
        loop_status="accepted",
        duration_s=3.0,
    )
    columns = trial_columns(row, RUN_ID, "digest", "S")
    assert columns["check_verdict"] is verdict
    assert columns["row_kind"] == kind
    assert columns["suite_digest"] == "digest"
    assert columns["try_end"] == "accepted"
    assert columns["try_ordinal"] == (0 if kind == "control" else ordinal)
