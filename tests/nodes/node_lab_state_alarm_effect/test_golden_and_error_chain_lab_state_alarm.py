# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the lab-state alarm (OMN-19998; plan figure W23).

Golden: a test source goes offline, an alarm is sent, its receipt is read back as delivered, and it clears only when the
source reads WORKING. Error: the channel is down, or accepts a send and never delivers, and the second channel takes the
alarm; the canary fails when the floor or the alarm path is silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelAlarmChannelDef,
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelCanaryInput,
    ModelLabSourceVerdict,
    ModelLabStateAlarmRequest,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    InMemoryAlarmChannel,
    UnconfiguredAlarmChannel,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_floor_canary import (
    HandlerFloorCanary,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
OVERLAY = ModelAlarmOverlay(
    readback_s=300,
    renotify_s=3600,
    heartbeat_max_age_s=900,
    channels=(
        ModelAlarmChannelDef(id="primary", kind="in_memory", role="primary"),
        ModelAlarmChannelDef(id="second", kind="in_memory", role="second"),
    ),
)


def verdict(source: str, state: str) -> ModelLabSourceVerdict:
    return ModelLabSourceVerdict.model_validate(
        {
            "id": source,
            "title": f"Source {source}",
            "kind": "hosts",
            "state": state,
            "probed_at": T0,
            "published_at": T0,
            "age_s": 0,
            "max_age_s": 900,
            "reason": "" if state == "WORKING" else "test runner offline",
            "summary": "",
            "subjects": (),
        }
    )


def request(
    state: ModelAlarmState, *verdicts: ModelLabSourceVerdict, at: int = 0
) -> ModelLabStateAlarmRequest:
    return ModelLabStateAlarmRequest(
        now=T0 + timedelta(seconds=at),
        verdicts=verdicts,
        state=state,
        overlay=OVERLAY,
    )


def test_golden_an_offline_source_raises_one_alarm_that_is_delivered_and_read_back() -> (
    None
):
    primary, second = InMemoryAlarmChannel(), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    first = handler.handle(request(ModelAlarmState(), verdict("hosts", "NOT WORKING")))
    assert first.raised == ("hosts",)
    assert len(primary.sent) == 1
    assert not second.sent
    # The read-back, one minute later, finds the receipt delivered.
    later = handler.handle(request(first.state, verdict("hosts", "NOT WORKING"), at=60))
    send = later.state.open["hosts"].sends[0]
    assert (send.delivery, send.receipt_id) == ("delivered", f"mem:{send.alarm_id}")
    assert later.undelivered == ()
    # Edge-triggered: the alarm is not raised again.
    assert later.raised == ()
    assert len(primary.sent) == 1
    assert later.state.heartbeat_at == T0 + timedelta(seconds=60)


def test_golden_the_alarm_clears_only_when_the_source_reads_working() -> None:
    handler = HandlerLabStateAlarm(
        {"primary": InMemoryAlarmChannel(), "second": InMemoryAlarmChannel()}
    )
    state = handler.handle(
        request(ModelAlarmState(), verdict("hosts", "UNKNOWN"))
    ).state
    still = handler.handle(request(state, verdict("hosts", "UNKNOWN"), at=30))
    assert "hosts" in still.state.open
    assert still.cleared == ()
    done = handler.handle(request(still.state, verdict("hosts", "WORKING"), at=60))
    assert done.cleared == ("hosts",)
    assert done.state.open == {}


def test_golden_a_source_that_fails_again_after_recovering_raises_again() -> None:
    primary = InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm(
        {"primary": primary, "second": InMemoryAlarmChannel()}
    )
    s1 = handler.handle(
        request(ModelAlarmState(), verdict("hosts", "NOT WORKING"))
    ).state
    s2 = handler.handle(request(s1, verdict("hosts", "WORKING"), at=30)).state
    again = handler.handle(request(s2, verdict("hosts", "NOT WORKING"), at=60))
    assert again.raised == ("hosts",)
    assert len(primary.sent) == 2


def test_error_a_channel_that_is_down_sends_the_alarm_to_the_second_channel_at_once() -> (
    None
):
    primary, second = InMemoryAlarmChannel(down=True), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    first = handler.handle(request(ModelAlarmState(), verdict("hosts", "NOT WORKING")))
    assert first.escalated == ("hosts",)
    sends = first.state.open["hosts"].sends
    assert [(s.role, s.delivery) for s in sends] == [
        ("primary", "failed"),
        ("second", "pending"),
    ]
    assert len(second.sent) == 1
    settled = handler.handle(
        request(first.state, verdict("hosts", "NOT WORKING"), at=30)
    )
    assert (
        settled.undelivered == ()
    )  # the second channel's receipt read back as delivered


def test_error_a_channel_that_accepts_and_never_delivers_escalates_after_five_minutes() -> (
    None
):
    primary, second = InMemoryAlarmChannel(black_hole=True), InMemoryAlarmChannel()
    handler = HandlerLabStateAlarm({"primary": primary, "second": second})
    first = handler.handle(request(ModelAlarmState(), verdict("hosts", "NOT WORKING")))
    assert first.escalated == ()
    assert first.undelivered == ("hosts",)
    early = handler.handle(
        request(first.state, verdict("hosts", "NOT WORKING"), at=299)
    )
    assert early.escalated == ()
    assert not second.sent
    late = handler.handle(request(early.state, verdict("hosts", "NOT WORKING"), at=300))
    assert late.escalated == ("hosts",)
    assert len(second.sent) == 1


def test_error_no_channel_delivering_leaves_the_alarm_undelivered_and_named() -> None:
    handler = HandlerLabStateAlarm(
        {
            "primary": UnconfiguredAlarmChannel("primary"),
            "second": InMemoryAlarmChannel(black_hole=True),
        }
    )
    first = handler.handle(request(ModelAlarmState(), verdict("hosts", "NOT WORKING")))
    refused = first.state.open["hosts"].sends[0]
    assert refused.delivery == "failed"
    assert "no authorised sender" in refused.detail
    assert first.undelivered == ("hosts",)


def test_error_the_canary_fails_when_the_floor_record_is_old_or_the_alarm_path_is_silent() -> (
    None
):
    canary = HandlerFloorCanary()

    def verdict_at(record_min: int | None, beat_min: int | None) -> tuple[str, bool]:
        out = canary.handle(
            ModelCanaryInput(
                now=T0,
                record_generated_at=None
                if record_min is None
                else T0 - timedelta(minutes=record_min),
                heartbeat_at=None
                if beat_min is None
                else T0 - timedelta(minutes=beat_min),
                floor_max_age_s=900,
                heartbeat_max_age_s=900,
            )
        )
        return out.state, out.notify

    assert verdict_at(2, 2) == ("ok", False)
    assert verdict_at(20, 2) == ("floor_silent", True)
    assert verdict_at(2, 20) == ("alarm_silent", True)
    assert verdict_at(20, 20) == ("both_silent", True)
    assert verdict_at(None, 2) == ("floor_silent", True)
    assert verdict_at(2, None) == ("alarm_silent", True)
    message = canary.handle(
        ModelCanaryInput(
            now=T0,
            record_generated_at=T0 - timedelta(minutes=20),
            heartbeat_at=None,
            floor_max_age_s=900,
            heartbeat_max_age_s=900,
        )
    ).message
    assert "20 min old" in message
    assert "never beaten" in message


def test_error_an_overlay_without_one_primary_and_one_second_channel_is_refused() -> (
    None
):
    with pytest.raises(ValueError, match="exactly one primary and one second"):
        ModelAlarmOverlay(
            readback_s=300,
            renotify_s=3600,
            heartbeat_max_age_s=900,
            channels=(
                ModelAlarmChannelDef(id="a", kind="in_memory", role="primary"),
                ModelAlarmChannelDef(id="b", kind="in_memory", role="primary"),
            ),
        )
