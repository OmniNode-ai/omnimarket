# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: every verdict morning_workflows_tick.sh gave a lab-fill fire (OMN-20768, OMN-20864, OMN-22866) is unchanged.

The old logic is a bash block and a python heredoc in a repository this one cannot import, so each row below is the old
block's verdict for one input, worked from the old source and spelled here (a cross-repo check that skipped where the
other clone is absent would be a skipped test).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.lab_fill.model_lab_fill_fire_outcome_policy import (
    ModelLabFillFireOutcomePolicy,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers.handler_lab_fill_fire_outcome import (
    HandlerLabFillFireOutcome,
)

from .conftest import LATEST_PATH, make_request, valid_result, with_result

pytestmark = pytest.mark.unit

ABSENT: dict[str, Any] = {
    "file_state": "absent",
    "file_nonempty": False,
    "file_text": None,
}
IDLE = {
    "free_slots": 3,
    "dispatched": 0,
    "idle_alarm": True,
    "alert_text": "LAB IDLE run=r details",
    "friction_recorded": True,
}
NO_RESULT_TEXT = (
    "LAB FILL NO RESULT fire=20261113T060000Z-lab-fill phase={phase} exit={exit}: the fire wrote no result, so its"
    " free slots and dispatches are unread; run log /state/runs/x.log"
)
IDLE_TEXT = (
    "floor row stoppage:lab-idle | {cause}free-slots=3 eligible={eligible} dispatched=0 | full reasons in "
    + LATEST_PATH
    + " | LAB IDLE run=r details"
)

# id: (request overrides, (phase, exit, degraded_class, channel, alert category, alert text))
PARITY: dict[str, tuple[dict[str, Any], tuple[Any, ...]]] = {
    "absent_completed": (ABSENT, ("result_invalid", 65, None, "absent", None, None)),
    "absent_failed_no_refusal": (
        {**ABSENT, "phase": "failed", "exit": 1},
        (
            "failed",
            1,
            "lab-fill-no-result",
            "absent",
            "lab_fill_no_result",
            NO_RESULT_TEXT.format(phase="failed", exit=1),
        ),
    ),
    "absent_timeout_refused": (
        {
            **ABSENT,
            "phase": "timeout",
            "exit": 124,
            "run_log_refused_line": "You've hit your weekly limit",
        },
        (
            "timeout",
            124,
            "lab-fill-claude-refused",
            "absent",
            "lab_fill_refused",
            "LAB FILL REFUSED fire=20261113T060000Z-lab-fill the Claude run was refused for a usage limit and"
            " dispatched nothing (free slots unread): You've hit your weekly limit",
        ),
    ),
    "absent_abandoned_background": (
        {**ABSENT, "phase": "abandoned_background", "exit": 0},
        (
            "abandoned_background",
            0,
            "lab-fill-no-result",
            "absent",
            "lab_fill_no_result",
            NO_RESULT_TEXT.format(phase="abandoned_background", exit=0),
        ),
    ),
    "absent_degraded_raises_no_alert": (
        {**ABSENT, "phase": "degraded", "exit": 70},
        ("degraded", 70, None, "absent", None, None),
    ),
    "idle_valid": (
        with_result(**IDLE),
        (
            "completed",
            0,
            None,
            "read",
            "lab_fill_idle",
            IDLE_TEXT.format(cause="", eligible="unread"),
        ),
    ),
    "idle_snapshot_absent": (
        {**with_result(**IDLE), "snapshot_present": False},
        (
            "completed",
            0,
            None,
            "read",
            "lab_fill_idle",
            IDLE_TEXT.format(
                cause="cause=linear-snapshot-absent:linear-2026-11-13.json | ",
                eligible="unread",
            ),
        ),
    ),
    "idle_eligible_key": (
        with_result(**IDLE, eligible=7),
        (
            "completed",
            0,
            None,
            "read",
            "lab_fill_idle",
            IDLE_TEXT.format(cause="", eligible=7),
        ),
    ),
    "blind": (
        with_result(**{**IDLE, "enumeration_failed": True}),
        (
            "degraded",
            70,
            "lab-fill-blind",
            "read",
            "lab_fill_blind",
            "LAB IDLE run=r details",
        ),
    ),
    "idle_friction_missing": (
        with_result(**{**IDLE, "friction_recorded": False}),
        (
            "result_invalid",
            65,
            "lab-idle-friction-missing",
            "read",
            "lab_fill_idle",
            IDLE_TEXT.format(cause="", eligible="unread"),
        ),
    ),
    "filled": (with_result(), ("completed", 0, None, "read", None, None)),
    "selftest": (
        {"selftest": True, **ABSENT},
        ("completed", 0, None, None, None, None),
    ),
}

INVALID = {
    "bad_json": {"file_text": "not json"},
    "not_a_dict": {"file_text": "[]"},
    "wrong_version": with_result(version=2),
    "wrong_workflow": with_result(workflow="dod-closeout-sweep"),
    "wrong_fire_id": with_result(fire_id="other"),
    "bool_free_slots": with_result(free_slots=True),
    "negative_dispatched": with_result(dispatched=-1),
    "idle_alarm_disagrees": with_result(idle_alarm=True, alert_text="x"),
    "missing_friction_flag": {
        "file_text": json.dumps(
            {k: v for k, v in valid_result().items() if k != "friction_recorded"}
        )
    },
    "newline_in_session_line": with_result(session_line="a\nb"),
    "idle_without_alert_text": with_result(**{**IDLE, "alert_text": ""}),
    "enumeration_failed_not_bool": with_result(enumeration_failed="yes"),
    "enumeration_failed_without_alarm": with_result(enumeration_failed=True),
}


@pytest.mark.parametrize("case", sorted(PARITY))
def test_fire_outcome_parity_with_the_old_tick(
    case: str, policy: ModelLabFillFireOutcomePolicy
) -> None:
    overrides, expected = PARITY[case]
    result = HandlerLabFillFireOutcome().handle(make_request(policy, **overrides))
    alert = result.alert
    channel = result.result_channel.value if result.result_channel else None
    assert (
        result.phase,
        result.exit,
        result.degraded_class,
        channel,
        alert.category.value if alert else None,
        alert.text if alert else None,
    ) == expected


@pytest.mark.parametrize("case", sorted(INVALID))
def test_fire_outcome_parity_invalid_result_is_result_invalid(
    case: str, policy: ModelLabFillFireOutcomePolicy
) -> None:
    result = HandlerLabFillFireOutcome().handle(make_request(policy, **INVALID[case]))
    assert (
        result.phase,
        result.exit,
        result.result_channel.value if result.result_channel else None,
    ) == (
        "result_invalid",
        65,
        "invalid",
    )
    assert result.alert is None
    assert result.latest_value is None


def test_fire_outcome_parity_undelivered_alert_degrades_idle_and_blind(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    for overrides in (
        with_result(**IDLE),
        with_result(**{**IDLE, "enumeration_failed": True}),
    ):
        result = HandlerLabFillFireOutcome().handle(
            make_request(policy, alert_exit=3, **overrides)
        )
        assert (result.phase, result.exit, result.degraded_class) == (
            "degraded",
            70,
            "lab-idle-alert-undelivered",
        )
        assert result.alert_exit == 3


def test_fire_outcome_parity_delivery_exit_does_not_change_a_refused_fire(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy,
            **ABSENT,
            phase="failed",
            exit=1,
            run_log_refused_line="usage limit reached",
            alert_exit=3,
        )
    )
    assert (result.phase, result.exit, result.alert_exit) == ("failed", 1, 3)


def test_fire_outcome_parity_a_filled_fire_ignores_the_delivery_exit(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(make_request(policy, alert_exit=3))
    assert (result.phase, result.exit, result.degraded_class) == ("completed", 0, None)


def test_fire_outcome_parity_the_judge_is_pure(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    request = make_request(policy, **with_result(**IDLE))
    handler = HandlerLabFillFireOutcome()
    assert handler.handle(request) == handler.handle(request)
