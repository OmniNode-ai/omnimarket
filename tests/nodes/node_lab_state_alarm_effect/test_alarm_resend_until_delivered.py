# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An open alarm with no delivery receipt is sent again until delivered (OMN-22478).

Six alarms opened at 16:07Z while the Slack sender was unconfigured. Both sends were refused, the alarm was marked
escalated, and the handler never sent an open alarm again, so the six stayed open with no delivery receipt after the
sender was configured. The cases below run that history through the real handler: refused sends under one handler, the
configured channels under another, over the same state.
"""

from __future__ import annotations

import pytest

from omnimarket.models.floor_17a import (
    ModelAlarmChannelDef,
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelLabStateAlarmRequest,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    InMemoryAlarmChannel,
    UnconfiguredAlarmChannel,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)
from tests.nodes.node_lab_state_alarm_effect.test_golden_and_error_chain_lab_state_alarm import (
    OVERLAY,
    request,
    verdict,
)

pytestmark = pytest.mark.unit

RENOTIFY = OVERLAY.renotify_s
BAD = verdict("hosts", "NOT WORKING")


def _unconfigured() -> HandlerLabStateAlarm:
    return HandlerLabStateAlarm(
        {
            "primary": UnconfiguredAlarmChannel("primary"),
            "second": UnconfiguredAlarmChannel("second"),
        }
    )


def test_an_open_alarm_refused_by_both_channels_is_sent_again_once_they_work() -> None:
    first = _unconfigured().handle(request(ModelAlarmState(), BAD, at=0))
    assert first.undelivered == ("hosts",)
    assert first.state.open["hosts"].escalated

    primary, second = InMemoryAlarmChannel(), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    retry = request(first.state, BAD, at=RENOTIFY)
    due = handler.handle(retry)
    assert len(primary.sent) == 1
    assert len(second.sent) == 1
    assert due.state.open["hosts"].last_notified_at == retry.now
    read_back = handler.handle(request(due.state, BAD, at=RENOTIFY + 30))
    assert read_back.undelivered == ()
    assert any(s.delivery == "delivered" for s in read_back.state.open["hosts"].sends)


def test_a_delivered_alarm_is_never_sent_again() -> None:
    primary, second = InMemoryAlarmChannel(), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    first = handler.handle(request(ModelAlarmState(), BAD, at=0))
    state = handler.handle(request(first.state, BAD, at=30)).state
    for tick in range(1, 6):
        state = handler.handle(request(state, BAD, at=tick * RENOTIFY)).state
    assert len(primary.sent) == 1
    assert not second.sent


def test_an_undelivered_alarm_waits_the_renotify_interval_between_sends() -> None:
    handler = _unconfigured()
    state = handler.handle(request(ModelAlarmState(), BAD, at=0)).state
    opened = state.open["hosts"].last_notified_at
    early = handler.handle(request(state, BAD, at=RENOTIFY - 1))
    assert early.state.open["hosts"].last_notified_at == opened
    retried = handler.handle(request(early.state, BAD, at=RENOTIFY))
    assert retried.state.open["hosts"].last_notified_at > opened


def test_a_channel_that_stays_down_does_not_grow_the_alarm_state() -> None:
    handler = _unconfigured()
    state = handler.handle(request(ModelAlarmState(), BAD, at=0)).state
    for tick in range(1, 8):
        state = handler.handle(request(state, BAD, at=tick * RENOTIFY)).state
    sends = state.open["hosts"].sends
    assert len(sends) == 2
    assert {s.channel for s in sends} == {"primary", "second"}
    assert all(s.delivery == "failed" for s in sends)


def test_a_send_still_inside_its_read_back_window_is_not_repeated() -> None:
    overlay = ModelAlarmOverlay.model_validate(
        {**OVERLAY.model_dump(mode="json"), "readback_s": 600, "renotify_s": 60}
    )
    primary = InMemoryAlarmChannel(black_hole=True)
    second = InMemoryAlarmChannel(black_hole=True)
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})

    def at(state: ModelAlarmState, seconds: int) -> ModelLabStateAlarmRequest:
        return request(state, BAD, at=seconds).model_copy(update={"overlay": overlay})

    state = handler.handle(at(ModelAlarmState(), 0)).state
    for seconds in (120, 240, 360, 480):
        state = handler.handle(at(state, seconds)).state
    assert len(primary.sent) == 1


def test_an_alarm_only_the_banner_delivered_still_reaches_the_primary_once_it_has_a_sender() -> (
    None
):
    # The state the six real alarms are in: the primary refused (no sender), the second channel delivered.
    refusing = HandlerLabStateAlarm(
        {
            "primary": UnconfiguredAlarmChannel("primary"),
            "second": InMemoryAlarmChannel(),
        }
    )
    first = refusing.handle(request(ModelAlarmState(), BAD, at=0))
    settled = refusing.handle(request(first.state, BAD, at=30))
    assert settled.undelivered == ()
    assert [s.delivery for s in settled.state.open["hosts"].sends] == [
        "failed",
        "delivered",
    ]

    primary, second = InMemoryAlarmChannel(), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    early = handler.handle(request(settled.state, BAD, at=RENOTIFY - 1))
    assert not primary.sent
    due = handler.handle(request(early.state, BAD, at=RENOTIFY))
    assert len(primary.sent) == 1
    assert not second.sent  # the second channel delivered; only the primary is owed
    read_back = handler.handle(request(due.state, BAD, at=RENOTIFY + 30))
    assert {s.role: s.delivery for s in read_back.state.open["hosts"].sends} == {
        "second": "delivered",
        "primary": "delivered",
    }
    for tick in range(2, 6):
        handler.handle(request(read_back.state, BAD, at=tick * RENOTIFY))
    assert len(primary.sent) == 1


def test_the_runtime_constructs_the_handler_with_no_channels_and_the_overlay_names_them() -> (
    None
):
    """The runtime builds a routed handler with no arguments (OMN-22478): the channels come from the request's overlay.

    A handler that demands its channels at construction was quarantined at boot, so the alarm operation could not be
    routed and the canonical-file-shape ratchet refused it as unbound. Built bare, with in-memory channels declared by
    the overlay, it raises the alarm and a later tick reads it back as delivered.
    """
    handler = HandlerLabStateAlarm()
    first = handler.handle(request(ModelAlarmState(), BAD, at=0))
    assert first.raised == ("hosts",)
    later = handler.handle(request(first.state, BAD, at=30))
    assert later.undelivered == ()
    assert any(s.delivery == "delivered" for s in later.state.open["hosts"].sends)


def test_a_bare_handler_with_a_slack_overlay_and_no_sender_refuses_with_a_typed_reason() -> (
    None
):
    """Error chain: the overlay names two slack channels and nothing supplies a sender, so both sends are refused."""
    overlay = OVERLAY.model_copy(
        update={
            "channels": (
                ModelAlarmChannelDef(
                    id="primary",
                    kind="slack",
                    role="primary",
                    token_ref="SLACK_BOT_TOKEN",
                    channel_ref="SLACK_ALERT_CHANNEL",
                ),
                ModelAlarmChannelDef(id="second", kind="in_memory", role="second"),
            )
        }
    )
    req = request(ModelAlarmState(), BAD, at=0).model_copy(update={"overlay": overlay})
    result = HandlerLabStateAlarm().handle(req)
    sends = result.state.open["hosts"].sends
    assert [s.delivery for s in sends if s.role == "primary"] == ["failed"]
    assert result.state.open["hosts"].escalated
    assert any(s.role == "second" for s in sends)
