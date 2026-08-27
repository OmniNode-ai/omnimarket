# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15373: fail-closed Linear git-automation drift guard.

Regression coverage for the 2026-07-28/29 incident: the ``Omninode`` team's
``GitAutomationState`` for ``event=merge`` named ``Done`` (type ``completed``)
with ``targetBranch=null``, so **every** PR merge in **every** repo flipped
**every** linked issue to Done in 2.0-3.1 seconds — no closing keyword, no
``dod_verify`` receipt, no evidence gate. 16 tickets were falsely completed in
one 4.5h window.

The setting was corrected by hand on 2026-07-29T02:29:19Z. A hand-corrected
workspace setting is not a mechanism; this guard is. These tests drive it with
the **verbatim recorded live payload** from the 2026-07-30 readback so the
parser is exercised against the real Linear response shape, not an idealised
one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_guard import (
    HandlerGitAutomationGuard,
    parse_automations,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    EnumGitAutomationVerdict,
    ModelGitAutomationException,
    ModelGitAutomationState,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_guard import (
    audit_git_automations,
    evaluate_git_automation,
    render_report,
)

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Recorded live payloads (verbatim from the Linear GraphQL API)
# ---------------------------------------------------------------------------

# The PRE-FIX shape: team Omninode's merge automation pointing at Done. Rebuilt
# from the OMN-15373 incident record — automation id f39a0bc8-...,
# state 2be3a0d0-... ("Done", type completed), targetBranch null.
_PRE_FIX_OMN = {
    "data": {
        "teams": {
            "nodes": [
                {
                    "id": "9bdff6a3-f4ef-4ff7-b29a-6c4cf44371e6",
                    "name": "Omninode",
                    "key": "OMN",
                    "gitAutomationStates": {
                        "nodes": [
                            {
                                "id": "84371122-7eae-462f-8e7e-0e17837aca0f",
                                "event": "start",
                                "state": {
                                    "id": "90882973-56af-4ef5-9958-5115a986e911",
                                    "name": "In Progress",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "f39a0bc8-19be-472d-acfe-84eee14ce9ab",
                                "event": "merge",
                                "state": {
                                    "id": "2be3a0d0-646a-4349-946e-ca395cbb0109",
                                    "name": "Done",
                                    "type": "completed",
                                },
                                "targetBranch": None,
                            },
                        ]
                    },
                }
            ]
        }
    }
}

# The 2026-07-30 live readback, verbatim: OMN is corrected (merge -> In Review),
# CON and JON still carry merge -> Done. Those two are recorded in OMN-15373 as
# out of scope pending an owner decision, so the guard is RED on arrival — by
# design. A guard that shipped green on a workspace that is still drifted would
# be proving nothing.
_LIVE_2026_07_30: dict[str, Any] = {
    "data": {
        "teams": {
            "nodes": [
                {
                    "id": "ef2198a9-350e-4a7a-adcf-fffe7ff6072a",
                    "name": "Contractors",
                    "key": "CON",
                    "gitAutomationStates": {
                        "nodes": [
                            {
                                "id": "bc6e479a-3c8f-4479-9899-3021b28725fd",
                                "event": "start",
                                "state": {
                                    "id": "3e929671-9135-48e3-9e68-96eb43df563e",
                                    "name": "In Progress",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "a403f948-0e2e-464f-b2a5-c5c3e35bccbd",
                                "event": "review",
                                "state": {
                                    "id": "625160a8-fc77-44ab-8c05-b3d409b24354",
                                    "name": "In Review",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "2ee841f5-f42d-408c-a00e-ddce2c763b58",
                                "event": "merge",
                                "state": {
                                    "id": "ebb41b18-6305-4084-8a4a-483178027336",
                                    "name": "Done",
                                    "type": "completed",
                                },
                                "targetBranch": None,
                            },
                        ]
                    },
                },
                {
                    "id": "12081a78-cec9-40c4-9d4c-027186ac205f",
                    "name": "JonahPrivate",
                    "key": "JON",
                    "gitAutomationStates": {
                        "nodes": [
                            {
                                "id": "e15b1293-f939-42a1-b62f-e9cc693684e8",
                                "event": "merge",
                                "state": {
                                    "id": "2d756fbc-7c99-426d-a93c-253b7236aec1",
                                    "name": "Done",
                                    "type": "completed",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "ba1b7b11-b756-4ca3-8804-11efba4dc624",
                                "event": "start",
                                "state": {
                                    "id": "782b5be7-595a-4822-8140-d42ae3b3188c",
                                    "name": "In Progress",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                        ]
                    },
                },
                {
                    "id": "9bdff6a3-f4ef-4ff7-b29a-6c4cf44371e6",
                    "name": "Omninode",
                    "key": "OMN",
                    "gitAutomationStates": {
                        "nodes": [
                            {
                                "id": "b2e8e30e-8c7c-4020-abfe-9cbb8e2d4fcf",
                                "event": "draft",
                                "state": {
                                    "id": "90882973-56af-4ef5-9958-5115a986e911",
                                    "name": "In Progress",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "f39a0bc8-19be-472d-acfe-84eee14ce9ab",
                                "event": "merge",
                                "state": {
                                    "id": "b9ea1b37-0da7-4dfd-a67a-5c57d8555f76",
                                    "name": "In Review",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                            {
                                "id": "049170a3-a4cf-4bc6-998f-8506f3bae7fe",
                                "event": "review",
                                "state": {
                                    "id": "b9ea1b37-0da7-4dfd-a67a-5c57d8555f76",
                                    "name": "In Review",
                                    "type": "started",
                                },
                                "targetBranch": None,
                            },
                        ]
                    },
                },
            ]
        }
    }
}


class _StubProbe:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def fetch(self) -> dict[str, Any]:
        return self._response


class _FailingProbe:
    def fetch(self) -> dict[str, Any]:
        raise RuntimeError(
            "Linear GraphQL error: [{'message': 'Authentication failed'}]"
        )


# ---------------------------------------------------------------------------
# RED: the pre-fix configuration is caught
# ---------------------------------------------------------------------------


def test_pre_fix_merge_to_done_is_drift() -> None:
    """The exact incident config — merge -> Done, all branches — must FAIL."""
    report = HandlerGitAutomationGuard(probe=_StubProbe(_PRE_FIX_OMN)).handle(now=_NOW)

    assert report.passed is False
    assert report.drift_count == 1
    drift = next(
        f for f in report.findings if f.verdict is EnumGitAutomationVerdict.DRIFT
    )
    assert drift.team_key == "OMN"
    assert drift.event == "merge"
    assert drift.state_name == "Done"
    assert drift.state_type == "completed"
    # targetBranch=null means every branch of every repo — the blast radius.
    assert drift.all_branches is True
    assert "dod_verify" in drift.reason


def test_pre_fix_non_merge_automations_are_clean() -> None:
    """Only the completed-type target is DRIFT; `start -> In Progress` is fine.

    A guard that condemned every automation would be a blanket denier and would
    be turned off. It must discriminate.
    """
    report = HandlerGitAutomationGuard(probe=_StubProbe(_PRE_FIX_OMN)).handle(now=_NOW)
    start = next(f for f in report.findings if f.event == "start")
    assert start.verdict is EnumGitAutomationVerdict.CLEAN


# ---------------------------------------------------------------------------
# GREEN: the corrected configuration passes
# ---------------------------------------------------------------------------


def test_post_fix_merge_to_in_review_passes() -> None:
    """The applied fix (merge -> In Review, type `started`) is CLEAN.

    This is the discriminating pair for the whole guard: same team, same
    automation id, same all-branches scope — only the target state's type
    differs — and the verdict inverts.
    """
    post_fix = json.loads(json.dumps(_PRE_FIX_OMN))
    merge = post_fix["data"]["teams"]["nodes"][0]["gitAutomationStates"]["nodes"][1]
    merge["state"] = {
        "id": "b9ea1b37-0da7-4dfd-a67a-5c57d8555f76",
        "name": "In Review",
        "type": "started",
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(post_fix)).handle(now=_NOW)

    assert report.passed is True
    assert report.drift_count == 0
    assert report.clean_count == 2


# ---------------------------------------------------------------------------
# The recorded live workspace: OMN clean, CON + JON still drifted
# ---------------------------------------------------------------------------


def test_recorded_live_readback_flags_exactly_con_and_jon() -> None:
    """2026-07-30 live state: the applied OMN fix holds; two teams still drift."""
    report = HandlerGitAutomationGuard(probe=_StubProbe(_LIVE_2026_07_30)).handle(
        now=_NOW
    )

    assert report.passed is False
    drifted = {
        (f.team_key, f.event)
        for f in report.findings
        if f.verdict is EnumGitAutomationVerdict.DRIFT
    }
    assert drifted == {("CON", "merge"), ("JON", "merge")}

    omn_merge = next(
        f for f in report.findings if f.team_key == "OMN" and f.event == "merge"
    )
    assert omn_merge.verdict is EnumGitAutomationVerdict.CLEAN
    assert omn_merge.state_name == "In Review"


def test_render_report_names_the_failing_teams() -> None:
    report = HandlerGitAutomationGuard(probe=_StubProbe(_LIVE_2026_07_30)).handle(
        now=_NOW
    )
    text = render_report(report)
    assert "RESULT: FAIL" in text
    assert "CON  merge" in text
    assert "JON  merge" in text


# ---------------------------------------------------------------------------
# Fail-closed boundaries
# ---------------------------------------------------------------------------


def test_probe_failure_fails_closed() -> None:
    """A transport/auth failure is FAIL, never 'no drift found'."""
    report = HandlerGitAutomationGuard(probe=_FailingProbe()).handle(now=_NOW)
    assert report.passed is False
    assert report.findings == []
    assert "probe failed" in report.failure_reason


def test_empty_automation_set_fails_closed_when_no_team_was_enumerated() -> None:
    """Zero automations AND zero teams proves nothing and must not read as clean.

    This is the difference between "checked and found nothing wrong" and
    "checked nothing". A revoked token, a renamed field, or a schema change all
    surface as an empty node list.
    """
    report = HandlerGitAutomationGuard(
        probe=_StubProbe({"data": {"teams": {"nodes": []}}})
    ).handle(now=_NOW)
    assert report.passed is False
    assert "ZERO automations" in report.failure_reason


def test_empty_automation_set_passes_when_teams_were_enumerated() -> None:
    """The post-OMN-16536-AC#1 steady state: teams still enumerable, every one
    carrying zero automations.

    On 2026-08-27 AC#1 deleted all ten mappings across OMN, CON and JON, making
    zero automations the *target* configuration. The original unconditional
    empty-set failure would have pinned this guard permanently red for the one
    reason that is not drift — so the scepticism moved to a positive control
    (at least one team enumerated) rather than being dropped.

    Verbatim live shape from the 2026-08-27 readback.
    """
    live_post_ac1 = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "ef2198a9-350e-4a7a-adcf-fffe7ff6072a",
                        "name": "Contractors",
                        "key": "CON",
                        "gitAutomationStates": {"nodes": []},
                    },
                    {
                        "id": "12081a78-cec9-40c4-9d4c-027186ac205f",
                        "name": "JonahPrivate",
                        "key": "JON",
                        "gitAutomationStates": {"nodes": []},
                    },
                    {
                        "id": "9bdff6a3-f4ef-4ff7-b29a-6c4cf44371e6",
                        "name": "Omninode",
                        "key": "OMN",
                        "gitAutomationStates": {"nodes": []},
                    },
                ]
            }
        }
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(live_post_ac1)).handle(now=_NOW)
    assert report.passed is True
    assert report.drift_count == 0


def test_probe_failure_still_fails_even_with_teams_enumerated() -> None:
    """The positive control relaxes ONLY the empty-set rule. A probe that raised
    still fails regardless — teams_enumerated is never reachable there."""
    report = HandlerGitAutomationGuard(probe=_FailingProbe()).handle(now=_NOW)
    assert report.passed is False
    assert "probe failed" in report.failure_reason


def test_unreadable_target_state_fails_closed() -> None:
    """An automation whose state block is missing is UNREADABLE, not CLEAN."""
    malformed = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "t1",
                        "key": "OMN",
                        "gitAutomationStates": {
                            "nodes": [
                                {"id": "a1", "event": "merge", "state": None},
                            ]
                        },
                    }
                ]
            }
        }
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(malformed)).handle(now=_NOW)
    assert report.passed is False
    assert report.unreadable_count == 1
    assert report.findings[0].verdict is EnumGitAutomationVerdict.UNREADABLE


def test_malformed_automation_is_not_silently_dropped() -> None:
    """Fail-closed parsing: a bad node shrinks nothing, it surfaces as UNREADABLE."""
    malformed = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "t1",
                        "key": "OMN",
                        "gitAutomationStates": {
                            "nodes": [
                                {"id": "a1", "event": "merge", "state": {"name": "?"}},
                                {
                                    "id": "a2",
                                    "event": "start",
                                    "state": {
                                        "id": "s",
                                        "name": "In Progress",
                                        "type": "started",
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    automations = parse_automations(malformed)
    assert len(automations) == 2
    assert automations[0].state_readable is False


# ---------------------------------------------------------------------------
# Accepted exceptions: dated, owned, and never permanent
# ---------------------------------------------------------------------------


def _con_merge() -> ModelGitAutomationState:
    return ModelGitAutomationState(
        team_key="CON",
        automation_id="2ee841f5-f42d-408c-a00e-ddce2c763b58",
        event="merge",
        state_id="ebb41b18-6305-4084-8a4a-483178027336",
        state_name="Done",
        state_type="completed",
    )


def test_live_exception_downgrades_drift_but_still_reports() -> None:
    exc = ModelGitAutomationException(
        automation_id="2ee841f5-f42d-408c-a00e-ddce2c763b58",
        team_key="CON",
        owner="Daniyal",
        reason="contractor lane; decision pending",
        expires_at=_NOW + timedelta(days=30),
    )
    report = audit_git_automations([_con_merge()], exceptions=[exc], now=_NOW)
    assert report.passed is True
    assert report.accepted_count == 1
    assert report.drift_count == 0
    # Still surfaced with the full drift explanation plus the acceptance.
    assert "ACCEPTED until" in report.findings[0].reason
    assert "Daniyal" in report.findings[0].reason


def test_expired_exception_stops_suppressing() -> None:
    """An exception is a dated decision, not a permanent silencer."""
    exc = ModelGitAutomationException(
        automation_id="2ee841f5-f42d-408c-a00e-ddce2c763b58",
        team_key="CON",
        owner="Daniyal",
        reason="contractor lane; decision pending",
        expires_at=_NOW - timedelta(seconds=1),
    )
    report = audit_git_automations([_con_merge()], exceptions=[exc], now=_NOW)
    assert report.passed is False
    assert report.drift_count == 1


def test_exception_for_a_different_automation_does_not_suppress() -> None:
    """Exceptions are pinned to one automation id — no wildcards, no team-wide
    blanket. Registering CON's merge automation must not cover JON's."""
    exc = ModelGitAutomationException(
        automation_id="e15b1293-f939-42a1-b62f-e9cc693684e8",  # JON's, not CON's
        team_key="JON",
        owner="Jonah",
        reason="private team",
        expires_at=_NOW + timedelta(days=30),
    )
    report = audit_git_automations([_con_merge()], exceptions=[exc], now=_NOW)
    assert report.passed is False
    assert report.drift_count == 1


@pytest.mark.parametrize(
    "state_type", ["started", "unstarted", "backlog", "triage", "canceled"]
)
def test_only_completed_type_is_drift(state_type: str) -> None:
    """Every non-completed type is CLEAN. `canceled` is included deliberately:
    it is a terminal type but it does not assert the work was DONE, so it makes
    no false completion claim."""
    automation = ModelGitAutomationState(
        team_key="OMN",
        automation_id="a1",
        event="merge",
        state_name="Some State",
        state_type=state_type,
    )
    finding = evaluate_git_automation(automation, now=_NOW)
    assert finding.verdict is EnumGitAutomationVerdict.CLEAN


def test_branch_scoped_completed_target_is_still_drift() -> None:
    """Narrowing the blast radius to one branch does not make it acceptable —
    it still writes Done with no proof, just less often."""
    automation = ModelGitAutomationState(
        team_key="OMN",
        automation_id="a1",
        event="merge",
        state_name="Done",
        state_type="completed",
        target_branch="main",
    )
    finding = evaluate_git_automation(automation, now=_NOW)
    assert finding.verdict is EnumGitAutomationVerdict.DRIFT
    assert finding.all_branches is False
    assert "branch pattern 'main'" in finding.reason


# ---------------------------------------------------------------------------
# Enumeration completeness (CodeRabbit finding on PR #2162)
# ---------------------------------------------------------------------------
#
# Once an empty automation set passes behind a positive team count, an
# unenumerated or unread team becomes a fail-open: it contributes zero
# automations while the run still reports a clean bill. These lock that shut.


def test_truncated_team_page_fails_closed() -> None:
    """hasNextPage on the outer page means teams were never looked at."""
    body = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "t1",
                        "key": "OMN",
                        "name": "Omninode",
                        "gitAutomationStates": {"nodes": []},
                    }
                ],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(body)).handle(now=_NOW)
    assert report.passed is False
    assert "truncation" in report.failure_reason


def test_team_with_unreadable_automation_connection_fails_closed() -> None:
    """A team whose gitAutomationStates.nodes is missing contributed zero
    automations for a reason that is NOT 'it has none'. It must not prop up the
    positive control."""
    body = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "t1",
                        "key": "OMN",
                        "name": "Omninode",
                        "gitAutomationStates": {"nodes": []},
                    },
                    {"id": "t2", "key": "CON", "name": "Contractors"},
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(body)).handle(now=_NOW)
    assert report.passed is False
    assert "CON" in report.failure_reason


def test_truncated_automation_page_fails_closed() -> None:
    """A full automation page cannot prove it was the last one."""
    body = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "t1",
                        "key": "OMN",
                        "name": "Omninode",
                        "gitAutomationStates": {
                            "nodes": [
                                {
                                    "id": f"a{i}",
                                    "event": "merge",
                                    "state": {
                                        "id": "s",
                                        "name": "In Review",
                                        "type": "started",
                                    },
                                    "targetBranch": None,
                                }
                                for i in range(10)
                            ],
                            "pageInfo": {"hasNextPage": True},
                        },
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    report = HandlerGitAutomationGuard(probe=_StubProbe(body)).handle(now=_NOW)
    assert report.passed is False
    assert "truncation" in report.failure_reason
