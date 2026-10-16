# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The external dead-man (OMN-20808, plan 4.8): what it raises and what it leaves quiet.

The mirrors are the two repository variables the watchdog legs write; the check reads their raw
text, so a missing, unparseable or mislabelled variable is judged the same as a stale one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.nodes.node_external_deadman_check_compute.handlers import (
    HandlerExternalDeadmanCheck,
)
from omnimarket.nodes.node_external_deadman_check_compute.models import (
    ISSUE_TITLE,
    EnumExternalDeadmanAction,
    EnumExternalDeadmanReason,
    ModelExternalDeadmanCheckRequest,
    ModelExternalDeadmanCheckResult,
    ModelExternalDeadmanIssue,
)

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)


def _ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (NOW + timedelta(**delta)).isoformat()


def _canary_done() -> dict[str, Any]:
    return {
        "injection_at": _ago(hours=3),
        "receipt_deadline": _ago(hours=2),
        "delivered": True,
        "recorded": True,
    }


def _mirror(leg: str, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "schema_version": "external-deadman-mirror/v1",
        "leg": leg,
        "written_at": _ago(minutes=3),
        "own_heartbeat_at": _ago(minutes=1),
        "peer_heartbeat_at": _ago(minutes=2),
        "undelivered": {"count": 0, "oldest_due_at": None},
        "unrecorded": {"count": 0, "oldest_due_at": None},
        "canary": _canary_done(),
    }
    body.update(overrides)
    return json.dumps(body)


def _run(
    primary: str | None,
    deadman: str | None,
    issues: tuple[ModelExternalDeadmanIssue, ...] = (),
) -> ModelExternalDeadmanCheckResult:
    return HandlerExternalDeadmanCheck().handle(
        ModelExternalDeadmanCheckRequest(
            now=NOW,
            primary_mirror=primary,
            deadman_mirror=deadman,
            open_issues=issues,
        )
    )


def _healthy() -> tuple[str, str]:
    return _mirror("primary"), _mirror("deadman")


def _codes(result: ModelExternalDeadmanCheckResult) -> set[EnumExternalDeadmanReason]:
    return {reason.code for reason in result.reasons}


# --- AC1: one issue, updated by the next stale run -------------------------------------------


def test_external_deadman_issue_once_first_stale_run_opens_one_issue() -> None:
    result = _run(None, None)

    assert result.alarm
    assert _codes(result) == {EnumExternalDeadmanReason.BOTH_MIRRORS_STALE}
    assert result.action is EnumExternalDeadmanAction.OPEN_ISSUE
    assert result.notify
    assert result.issue_title == ISSUE_TITLE
    assert result.issue_number is None
    assert "both_mirrors_stale" in result.issue_body
    assert result.slack_text


def test_external_deadman_issue_once_second_stale_run_updates_the_issue() -> None:
    first = _run(None, None)
    existing = ModelExternalDeadmanIssue(
        number=41, title=first.issue_title, body=first.issue_body
    )

    second = _run(None, None, (existing,))

    assert second.alarm
    assert second.action is EnumExternalDeadmanAction.UPDATE_ISSUE
    assert second.issue_number == 41
    # The same reasons again: the issue body is refreshed, nobody is paged a second time.
    assert not second.notify


def test_external_deadman_issue_once_a_new_reason_notifies_again() -> None:
    first = _run(None, None)
    existing = ModelExternalDeadmanIssue(
        number=41, title=first.issue_title, body=first.issue_body
    )

    fresh_but_undelivered = _mirror(
        "primary", undelivered={"count": 1, "oldest_due_at": _ago(hours=2)}
    )
    third = _run(fresh_but_undelivered, None, (existing,))

    assert third.action is EnumExternalDeadmanAction.UPDATE_ISSUE
    assert third.issue_number == 41
    assert third.notify


def test_external_deadman_issue_once_never_opens_beside_an_open_issue() -> None:
    older = ModelExternalDeadmanIssue(number=12, title=ISSUE_TITLE, body="stale text")
    newer = ModelExternalDeadmanIssue(number=19, title=ISSUE_TITLE, body="stale text")

    result = _run(None, None, (newer, older))

    assert result.action is EnumExternalDeadmanAction.UPDATE_ISSUE
    assert result.issue_number == 12


def test_external_deadman_issue_once_an_unrelated_issue_is_not_ours() -> None:
    unrelated = ModelExternalDeadmanIssue(number=3, title="Something else", body="")

    result = _run(None, None, (unrelated,))

    assert result.action is EnumExternalDeadmanAction.OPEN_ISSUE


def test_external_deadman_issue_once_stale_means_older_than_twenty_minutes() -> None:
    at_bound = (
        _mirror("primary", written_at=_ago(minutes=20)),
        _mirror("deadman", written_at=_ago(minutes=20)),
    )
    past_bound = (
        _mirror("primary", written_at=_ago(minutes=20, seconds=1)),
        _mirror("deadman", written_at=_ago(minutes=20, seconds=1)),
    )

    assert not _run(*at_bound).alarm
    assert _codes(_run(*past_bound)) == {EnumExternalDeadmanReason.BOTH_MIRRORS_STALE}


def test_external_deadman_issue_once_an_unreadable_variable_is_a_stale_mirror() -> None:
    healthy_deadman = _mirror("deadman")
    cases: dict[str, str | None] = {
        "missing": None,
        "empty": "   ",
        "not json": "{not json",
        "not an object": "[1, 2]",
        "wrong leg": _mirror("deadman"),
        "wrong schema": _mirror("primary", schema_version="other/v9"),
        "backlog without a due time": _mirror(
            "primary", undelivered={"count": 3, "oldest_due_at": None}
        ),
        "naive timestamp": _mirror("primary", written_at="2026-10-15T11:57:00"),
    }
    stale_deadman = _mirror("deadman", written_at=_ago(hours=5))

    for name, primary in cases.items():
        result = _run(primary, stale_deadman)
        assert _codes(result) == {EnumExternalDeadmanReason.BOTH_MIRRORS_STALE}, name
    # The control: the same deadman variable beside a readable primary is not an alarm by itself.
    assert not _run(_mirror("primary"), healthy_deadman).alarm


# --- AC2: an undelivered episode or a half-complete canary in a fresh mirror -----------------


def test_external_deadman_escalates_undelivered_episode_past_its_bound() -> None:
    primary = _mirror(
        "primary", undelivered={"count": 2, "oldest_due_at": _ago(minutes=31)}
    )

    result = _run(primary, _mirror("deadman"))

    assert result.alarm
    assert result.action is EnumExternalDeadmanAction.OPEN_ISSUE
    assert _codes(result) == {EnumExternalDeadmanReason.EPISODE_UNDELIVERED}
    assert result.reasons[0].leg == "primary"
    assert "2" in result.reasons[0].detail


def test_external_deadman_escalates_undelivered_unrecorded_on_the_other_leg() -> None:
    deadman = _mirror(
        "deadman", unrecorded={"count": 1, "oldest_due_at": _ago(hours=1)}
    )

    result = _run(_mirror("primary"), deadman)

    assert _codes(result) == {EnumExternalDeadmanReason.EPISODE_UNRECORDED}
    assert result.reasons[0].leg == "deadman"


def test_external_deadman_escalates_undelivered_half_complete_canary() -> None:
    missing_delivered = {**_canary_done(), "delivered": False}
    missing_recorded = {**_canary_done(), "recorded": False}

    delivered = _run(_mirror("primary", canary=missing_delivered), _mirror("deadman"))
    recorded = _run(_mirror("primary"), _mirror("deadman", canary=missing_recorded))

    assert _codes(delivered) == {EnumExternalDeadmanReason.CANARY_INCOMPLETE}
    assert "delivered" in delivered.reasons[0].detail
    assert "recorded" not in delivered.reasons[0].detail
    assert delivered.action is EnumExternalDeadmanAction.OPEN_ISSUE
    assert _codes(recorded) == {EnumExternalDeadmanReason.CANARY_INCOMPLETE}
    assert "recorded" in recorded.reasons[0].detail
    assert recorded.reasons[0].leg == "deadman"


def test_external_deadman_escalates_undelivered_only_from_a_fresh_mirror() -> None:
    stale_with_backlog = _mirror(
        "primary",
        written_at=_ago(hours=4),
        undelivered={"count": 9, "oldest_due_at": _ago(hours=6)},
    )

    result = _run(stale_with_backlog, _mirror("deadman"))

    assert not result.alarm


# --- AC3: nothing to raise ------------------------------------------------------------------


def test_external_deadman_quiet_when_healthy() -> None:
    result = _run(*_healthy())

    assert not result.alarm
    assert result.reasons == ()
    assert result.action is EnumExternalDeadmanAction.NONE
    assert not result.notify
    assert result.issue_body == ""
    assert result.slack_text == ""


def test_external_deadman_quiet_when_healthy_day_before_its_injection() -> None:
    not_yet = {
        "injection_at": _ahead(hours=14),
        "receipt_deadline": _ahead(hours=15),
        "delivered": False,
        "recorded": False,
    }

    result = _run(
        _mirror("primary", canary=not_yet), _mirror("deadman", canary=not_yet)
    )

    assert not result.alarm


def test_external_deadman_quiet_when_healthy_inside_the_receipt_budget() -> None:
    inside = {
        "injection_at": _ago(minutes=10),
        "receipt_deadline": _ahead(minutes=10),
        "delivered": False,
        "recorded": False,
    }
    at_deadline = {**inside, "receipt_deadline": NOW.isoformat()}
    past = {**inside, "receipt_deadline": _ago(seconds=1)}

    assert not _run(_mirror("primary", canary=inside), _mirror("deadman")).alarm
    assert not _run(_mirror("primary", canary=at_deadline), _mirror("deadman")).alarm
    # The control: one second past the deadline the same state is an alarm.
    assert _codes(_run(_mirror("primary", canary=past), _mirror("deadman"))) == {
        EnumExternalDeadmanReason.CANARY_INCOMPLETE
    }


def test_external_deadman_quiet_when_healthy_backlog_inside_its_escalation_bound() -> (
    None
):
    primary = _mirror(
        "primary", undelivered={"count": 1, "oldest_due_at": _ago(minutes=29)}
    )
    at_bound = _mirror(
        "deadman", unrecorded={"count": 1, "oldest_due_at": _ago(minutes=30)}
    )

    assert not _run(primary, at_bound).alarm


def test_external_deadman_quiet_when_healthy_one_stale_mirror_alone_is_not_the_alarm() -> (
    None
):
    stale = _mirror("primary", written_at=_ago(hours=3))

    assert not _run(stale, _mirror("deadman")).alarm
    assert not _run(_mirror("primary"), None).alarm


def test_external_deadman_quiet_when_healthy_does_not_touch_an_open_issue() -> None:
    open_issue = ModelExternalDeadmanIssue(
        number=5, title=ISSUE_TITLE, body="earlier alarm"
    )

    result = _run(*_healthy(), (open_issue,))

    assert not result.alarm
    assert result.action is EnumExternalDeadmanAction.NONE
    assert result.issue_number is None


def test_external_deadman_quiet_when_healthy_a_mirror_without_canary_state_makes_no_claim() -> (
    None
):
    no_canary = {"canary": None}

    result = _run(_mirror("primary", **no_canary), _mirror("deadman", **no_canary))

    assert not result.alarm
