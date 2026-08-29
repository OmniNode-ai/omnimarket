# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert channel must be provably alive between alerts (OMN-15600).

Epic OMN-16776 Phase 1, gate item 5.  Every other item on that gate went PASS
on 2026-08-29T05:35Z; this one did not, and the epic's own words for why are::

    It proves #omninode-notifications accepted a message at 05:27Z ... It does
    NOT satisfy item 5, which asks for a *checker* that reports FAILURE when
    pointed at a known-dead channel, proven RED first.  No such checker exists
    in omnimarket/src, omnibase_infra/src, or the test trees.  Until it does, a
    channel that goes dead between alerts still goes dead silently.

These tests drive the shipped classifier directly.  Each one names the concrete
way an alert channel dies in production:

* ``SLACK_WEBHOOK_URL`` died as ``HTTP 404 / no_service`` and every consumer
  read it as success because nothing looked past the transport.
* The bot-token path that replaced it has the SAME trap in a nastier shape:
  Slack answers ``HTTP 200`` with ``{"ok": false}`` for ``channel_not_found``,
  ``not_in_channel``, ``invalid_auth`` and ``token_revoked``.  A status-code
  check scores every one of those as delivered.
* ``probe_channel()`` in omniclaude wrapped its own body in ``except
  Exception`` and returned ``"unknown"``, which both of its consumers treated
  as not-failed (OMN-15606).  A detector that fails OPEN reports clean while
  the channel is dead, which is worse than no detector at all.

So: three failure shapes, and this classifier must return a NON-healthy verdict
for all three, distinguishing each from the others and from "never configured".
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.classify_channel_probe import (
    classify_channel_probe,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.models import (
    EnumAlertChannelStatus,
    ModelAlertChannelObservation,
)

pytestmark = pytest.mark.unit


def _healthy() -> ModelAlertChannelObservation:
    """An observation of a channel that is genuinely able to receive an alert."""
    return ModelAlertChannelObservation(
        credentials_present=True,
        auth_ok=True,
        channel_ok=True,
        bot_is_member=True,
    )


# ---------------------------------------------------------------------------
# The load-bearing case: HTTP 200 carrying ok=false
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slack_error",
    [
        "channel_not_found",
        "not_in_channel",
        "invalid_auth",
        "token_revoked",
        "account_inactive",
        "is_archived",
    ],
)
def test_http_200_carrying_ok_false_is_dead_never_live(slack_error: str) -> None:
    """Every ``ok:false`` Slack answers with HTTP 200 must classify as DEAD.

    This is the whole ticket in one assertion.  The transport succeeded, the
    status code was 200, and the channel cannot receive an alert.
    """
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=False,
            channel_error=slack_error,
        )
    )
    assert verdict.status is EnumAlertChannelStatus.DEAD
    assert verdict.healthy is False
    assert verdict.slack_error == slack_error


def test_a_revoked_token_is_dead_even_before_the_channel_is_looked_at() -> None:
    """``auth.test`` answering ok=false ends the probe at DEAD."""
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=False,
            auth_error="token_revoked",
        )
    )
    assert verdict.status is EnumAlertChannelStatus.DEAD
    assert verdict.slack_error == "token_revoked"


def test_bot_outside_the_channel_is_dead_although_every_call_said_ok() -> None:
    """``is_member: false`` is a dead delivery path with two ``ok:true`` bodies.

    ``auth.test`` passes (the token is valid) and ``conversations.info`` passes
    (the channel exists).  ``chat.postMessage`` would then answer HTTP 200 with
    ``{"ok": false, "error": "not_in_channel"}`` — the alert is lost, and both
    probes said ok.  A checker that stopped at ``ok`` would call this LIVE.
    """
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=False,
        )
    )
    assert verdict.status is EnumAlertChannelStatus.DEAD
    assert verdict.healthy is False
    assert verdict.slack_error == "not_in_channel"


# ---------------------------------------------------------------------------
# Fail CLOSED: the OMN-15606 regression
# ---------------------------------------------------------------------------


def test_a_probe_that_could_not_run_is_probe_error_and_is_not_healthy() -> None:
    """The detector's own failure is never reported as a healthy channel.

    OMN-15606: ``probe_channel()`` returned ``"unknown"`` on any exception and
    both consumers treated it as not-failed, so the detector reported clean
    while the channel may have been dead.
    """
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(
            credentials_present=True,
            transport_error="ClientConnectorError: [Errno 8] nodename nor servname",
        )
    )
    assert verdict.status is EnumAlertChannelStatus.PROBE_ERROR
    assert verdict.healthy is False


def test_a_missing_answer_is_probe_error_not_live() -> None:
    """No transport error, but no answer either — still not healthy.

    An observation with nothing to judge cannot produce LIVE.  A classifier
    whose default branch is "healthy" is the fail-open shape this node exists
    to end.
    """
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(credentials_present=True)
    )
    assert verdict.status is EnumAlertChannelStatus.PROBE_ERROR
    assert verdict.healthy is False


def test_no_observation_of_any_shape_reports_healthy_unless_every_check_passed() -> (
    None
):
    """Exhaustive: LIVE requires auth ok AND channel ok AND membership.

    Enumerates the eight combinations of the three tri-state answers that a
    reachable, configured channel can produce and asserts exactly one of them
    is healthy.  A future edit that adds a permissive branch fails here rather
    than in production six weeks later.
    """
    healthy_seen = 0
    for auth_ok in (True, False, None):
        for channel_ok in (True, False, None):
            for is_member in (True, False, None):
                verdict = classify_channel_probe(
                    ModelAlertChannelObservation(
                        credentials_present=True,
                        auth_ok=auth_ok,
                        channel_ok=channel_ok,
                        bot_is_member=is_member,
                    )
                )
                if verdict.healthy:
                    healthy_seen += 1
                    assert auth_ok is True
                    assert channel_ok is True
                    assert is_member is True
                    assert verdict.status is EnumAlertChannelStatus.LIVE
    assert healthy_seen == 1


# ---------------------------------------------------------------------------
# Set-but-dead is a distinct state from unset (the ticket's AC5)
# ---------------------------------------------------------------------------


def test_unconfigured_is_its_own_state_and_is_not_dead_and_is_not_live() -> None:
    """Three states where the shell call sites had two.

    The three omniclaude call sites branched only on empty/non-empty, so a
    credential that was *set and dead* was indistinguishable from a healthy
    one.  NOT_CONFIGURED must be neither LIVE nor DEAD: the operator action for
    each is different, and collapsing them loses which one to take.
    """
    verdict = classify_channel_probe(
        ModelAlertChannelObservation(credentials_present=False)
    )
    assert verdict.status is EnumAlertChannelStatus.NOT_CONFIGURED
    assert verdict.status is not EnumAlertChannelStatus.DEAD
    assert verdict.healthy is False


def test_the_four_states_are_distinct_members_of_one_enum() -> None:
    """No stringly-typed status, and no fifth state smuggled in later."""
    assert {member.value for member in EnumAlertChannelStatus} == {
        "LIVE",
        "DEAD",
        "NOT_CONFIGURED",
        "PROBE_ERROR",
    }


def test_only_live_is_healthy_across_the_whole_enum() -> None:
    """``healthy`` is derived from the status, not carried independently."""
    for status in EnumAlertChannelStatus:
        assert status.is_healthy is (status is EnumAlertChannelStatus.LIVE)


def test_the_healthy_case_still_passes() -> None:
    """The control. A checker that can only fail is not a checker either."""
    verdict = classify_channel_probe(_healthy())
    assert verdict.status is EnumAlertChannelStatus.LIVE
    assert verdict.healthy is True
    assert verdict.slack_error is None
