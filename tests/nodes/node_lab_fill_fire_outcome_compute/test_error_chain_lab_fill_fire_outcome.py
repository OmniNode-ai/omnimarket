# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a fire its Claude run never carried out is judged and alerted, never read as nothing to judge (OMN-20864).

On 2026-10-15 every h200 fire from about 00:30Z to 02:50Z logged only "You've hit your weekly limit", wrote no result
file and raised no alert. The named injected break is the run-log line cut out: the same fire must then read as a
no-result fire, not as a refusal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.lab_fill.enum_lab_fill_fire_outcome import (
    EnumLabFillAlertCategory,
)
from omnimarket.models.lab_fill.model_lab_fill_fire_outcome_policy import (
    ModelLabFillFireOutcomePolicy,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers.handler_lab_fill_fire_outcome import (
    HandlerLabFillFireOutcome,
    first_refused_line,
    first_word_marker_seen,
)

from .conftest import POLICY_VALUES, make_request

pytestmark = pytest.mark.unit

ABSENT = {"file_state": "absent", "file_nonempty": False, "file_text": None}
LOG = "starting\nYou've hit your weekly limit · resets Oct 15\r\nexit\n"


def test_error_chain_a_limit_refused_fire_alerts_as_refused(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    line = first_refused_line(LOG, policy)
    assert line == "You've hit your weekly limit · resets Oct 15"
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy, **ABSENT, phase="failed", exit=1, run_log_refused_line=line
        )
    )
    assert result.degraded_class == "lab-fill-claude-refused"
    assert result.alert is not None
    assert result.alert.category is EnumLabFillAlertCategory.REFUSED


def test_error_chain_injected_break_run_log_line_cut_reads_as_no_result(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    line = first_refused_line("starting\nexit\n", policy)
    assert line == ""
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy, **ABSENT, phase="failed", exit=1, run_log_refused_line=line
        )
    )
    assert result.degraded_class == "lab-fill-no-result"
    assert result.alert is not None
    assert result.alert.category is EnumLabFillAlertCategory.NO_RESULT


def test_error_chain_the_refused_line_is_cut_to_the_policy_length(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    assert len(first_refused_line("hit your " + "x" * 400 + " limit", policy)) == 160


@pytest.mark.parametrize(
    ("log", "seen"),
    [
        ("WORKFLOW_RESULT_TRUNCATED", True),
        ("WORKFLOW_RESULT_TRUNCATED the result ends mid-structure", True),
        ("the agent said WORKFLOW_RESULT_TRUNCATED later", False),
        ("WORKFLOW_RESULT_TRUNCATEDX", False),
        ("", False),
    ],
)
def test_error_chain_marker_counts_only_as_a_first_word(log: str, seen: bool) -> None:
    assert first_word_marker_seen(log, "WORKFLOW_RESULT_TRUNCATED") is seen


def test_error_chain_the_policy_refuses_a_missing_setting() -> None:
    values = {k: v for k, v in POLICY_VALUES.items() if k != "degraded_exit"}
    with pytest.raises(ValidationError):
        ModelLabFillFireOutcomePolicy.model_validate(values)


def test_error_chain_the_request_refuses_an_unknown_field(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    with pytest.raises(ValidationError):
        make_request(policy, surprise=1)
