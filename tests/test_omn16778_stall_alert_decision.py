# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The firing decision, driven through the real handler (OMN-16778).

RED baseline this replaces: on 2026-08-27 the delegation chain died and
produced 16 quarantined commands in about a minute, and nothing fired. The
outage was found by hand, hop by hop, by someone running an unrelated
verification. Every test here is a case that produced silence before this node
existed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers import (
    build_slack_command,
    decide_stall_alert,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    EnumStallAlertOutcome,
    EnumStallAlertSeverity,
    ModelConsumerFlowStallAlertRequest,
    ModelFlowWindowObservation,
    ModelStallAlertPolicy,
    load_stall_alert_policy,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_consumer_flow_stall_alert_effect"
    / "contract.yaml"
)

_EPOCH = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(minutes=1)


@pytest.fixture
def policy() -> ModelStallAlertPolicy:
    """The shipped policy, read from the contract — never a literal here."""
    return load_stall_alert_policy(CONTRACT_PATH)


def _window(
    index: int,
    state: EnumConsumerFlowState,
    *,
    messages_in: int | None = None,
    messages_out: int | None = None,
    messages_dlq: int | None = None,
    handler_errors: int | None = None,
) -> ModelFlowWindowObservation:
    """One window. A STALLED window defaults to carrying failure evidence.

    OMN-16778: the contract lists STALLED under ``require_failure_evidence_for``
    because an out-count of zero is the normal shape of a healthy projection or
    of a compute that delivers its intent in-process. A STALLED window with no
    dead-letter and no handler error is therefore NOT admissible evidence, and
    these fixtures state the failure they are describing rather than relying on
    a bare state name. ``test_a_stalled_window_without_failure_evidence_is_not_a_stall``
    pins the other half.
    """
    if state is EnumConsumerFlowState.STALLED and messages_dlq is None:
        messages_dlq = messages_in
        handler_errors = messages_in if handler_errors is None else handler_errors
    start = _EPOCH + index * _WINDOW
    return ModelFlowWindowObservation(
        window_start=start,
        window_end=start + _WINDOW,
        flow_state=state,
        messages_in=messages_in,
        messages_out=messages_out,
        messages_dlq=messages_dlq,
        handler_errors=handler_errors,
    )


def _request(
    windows: tuple[ModelFlowWindowObservation, ...],
    policy: ModelStallAlertPolicy,
    *,
    consumer_group: str = "node_gateway_link_health_projection_compute",
    topic: str = "onex.evt.platform.node-heartbeat.v1",
) -> ModelConsumerFlowStallAlertRequest:
    return ModelConsumerFlowStallAlertRequest(
        consumer_group=consumer_group,
        topic=topic,
        node_id=uuid4(),
        correlation_id=uuid4(),
        windows=windows,
        policy=policy,
    )


@pytest.mark.unit
def test_confirmed_stall_run_fires_a_fail_alert(policy: ModelStallAlertPolicy) -> None:
    """AC1 — the OMN-16755 shape (everything in, nothing out) alerts.

    15,750 messages consumed, zero produced, LAG 0, group Stable. Every check
    the platform had was green. This is the case the alert exists for.
    """
    windows = tuple(
        _window(i, EnumConsumerFlowState.STALLED, messages_in=15750, messages_out=0)
        for i in range(policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))

    assert decision.outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert decision.severity is EnumStallAlertSeverity.FAIL
    assert decision.should_publish is True
    assert decision.alert is not None
    assert decision.alert.consumer_group == (
        "node_gateway_link_health_projection_compute"
    )
    assert decision.consecutive_alerting_windows == policy.confirm_windows


@pytest.mark.unit
def test_starved_run_also_fires(policy: ModelStallAlertPolicy) -> None:
    """STARVED is an alerting state too — the gateway-forwarder inbound leg."""
    windows = tuple(
        _window(i, EnumConsumerFlowState.STARVED, messages_in=0, messages_out=0)
        for i in range(policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert decision.alert is not None
    assert decision.alert.flow_state is EnumConsumerFlowState.STARVED


@pytest.mark.unit
def test_a_single_stalled_window_does_not_fire(
    policy: ModelStallAlertPolicy,
) -> None:
    """Below the confirm threshold is silent — this is the anti-flap half.

    Firing on one window reproduces the alert flap the .201 host reporter
    produced before OMN-16789 damped it, and an alert channel that flaps is
    muted within a day.
    """
    windows = (
        _window(0, EnumConsumerFlowState.FLOWING, messages_in=10, messages_out=10),
        _window(1, EnumConsumerFlowState.STALLED, messages_in=10, messages_out=0),
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.PENDING_CONFIRMATION
    assert decision.should_publish is False
    assert decision.alert is None


@pytest.mark.unit
def test_idle_consumer_on_a_quiet_topic_never_alerts(
    policy: ModelStallAlertPolicy,
) -> None:
    """AC3 — the false-positive half, and it is not optional.

    A stall alert that fires on idleness gets muted within a day and is then
    worth nothing. Falsified by an alert storm on quiet topics.
    """
    windows = tuple(
        _window(i, EnumConsumerFlowState.IDLE, messages_in=0, messages_out=0)
        for i in range(policy.clear_windows + policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.NO_ALERT
    assert decision.severity is EnumStallAlertSeverity.NONE
    assert decision.should_publish is False


@pytest.mark.unit
def test_flowing_consumer_never_alerts(policy: ModelStallAlertPolicy) -> None:
    """A healthy seam says nothing at all."""
    windows = tuple(
        _window(i, EnumConsumerFlowState.FLOWING, messages_in=7, messages_out=7)
        for i in range(policy.clear_windows + policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.NO_ALERT
    assert decision.should_publish is False


@pytest.mark.unit
def test_unknown_window_warns_and_does_not_fire_a_stall_alert(
    policy: ModelStallAlertPolicy,
) -> None:
    """AC5 — UNKNOWN is neither healthy nor stalled, and it is not silent.

    Falsified by UNKNOWN being treated as either. A missed heartbeat is the
    absence of an observation; the counters stay None rather than becoming 0.
    """
    windows = (
        _window(0, EnumConsumerFlowState.FLOWING, messages_in=3, messages_out=3),
        _window(1, EnumConsumerFlowState.UNKNOWN),
    )
    decision = decide_stall_alert(_request(windows, policy))

    assert decision.outcome is EnumStallAlertOutcome.WARN_MISSED_WINDOW
    assert decision.severity is EnumStallAlertSeverity.WARN
    assert decision.should_publish is policy.deliver_warnings
    assert decision.alert is None
    assert decision.consecutive_unknown_windows == 1
    assert windows[1].messages_in is None, (
        "an unobserved window must not be coerced to zero — that is the "
        "false-green OMN-16777 exists to close"
    )


@pytest.mark.unit
def test_an_unknown_window_breaks_a_stall_run_instead_of_extending_it(
    policy: ModelStallAlertPolicy,
) -> None:
    """A dropped heartbeat is not evidence the stall continued through it.

    Counting it as a continuation would let a runtime that stopped heartbeating
    manufacture a confirmed alert out of nothing.
    """
    windows = (
        _window(0, EnumConsumerFlowState.STALLED, messages_in=5, messages_out=0),
        _window(1, EnumConsumerFlowState.UNKNOWN),
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.WARN_MISSED_WINDOW
    assert decision.consecutive_alerting_windows == 0


@pytest.mark.unit
def test_recently_recovered_consumer_is_recovering_not_cleared(
    policy: ModelStallAlertPolicy,
) -> None:
    """Hysteresis: a stall that blips healthy for one window has not recovered."""
    windows = (
        *(
            _window(i, EnumConsumerFlowState.STALLED, messages_in=9, messages_out=0)
            for i in range(policy.confirm_windows)
        ),
        _window(
            policy.confirm_windows,
            EnumConsumerFlowState.FLOWING,
            messages_in=9,
            messages_out=9,
        ),
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.RECOVERING
    assert decision.should_publish is False


@pytest.mark.unit
def test_pending_stall_that_flows_is_not_reported_as_recovering(
    policy: ModelStallAlertPolicy,
) -> None:
    """A stall that never confirmed does not enter the recovery branch."""
    windows = (
        _window(0, EnumConsumerFlowState.STALLED, messages_in=9, messages_out=0),
        _window(1, EnumConsumerFlowState.FLOWING, messages_in=9, messages_out=9),
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.NO_ALERT
    assert decision.should_publish is False


@pytest.mark.unit
def test_alert_payload_names_consumer_topic_counts_and_run_length(
    policy: ModelStallAlertPolicy,
) -> None:
    """AC4 — falsified by a message that needs a human to go run ``rpk``."""
    windows = tuple(
        _window(
            i,
            EnumConsumerFlowState.STALLED,
            messages_in=15750,
            messages_out=0,
            messages_dlq=16,
            handler_errors=16,
        )
        for i in range(policy.confirm_windows)
    )
    request = _request(windows, policy)
    decision = decide_stall_alert(request)
    assert decision.alert is not None
    assert decision.idempotency_key is not None

    command = build_slack_command(
        payload=decision.alert,
        channel="C08PRL6BRQE",
        idempotency_key=decision.idempotency_key,
        correlation_id=request.correlation_id,
    )
    assert command.text is not None
    for expected in (
        request.consumer_group,
        request.topic,
        "15750",
        "dlq=16",
        str(policy.confirm_windows),
        str(request.correlation_id),
    ):
        assert expected in command.text, f"alert text omits {expected!r}"


@pytest.mark.unit
def test_a_standing_stall_reuses_one_idempotency_key_inside_the_renotify_window(
    policy: ModelStallAlertPolicy,
) -> None:
    """A condition that stays broken must not re-post on every heartbeat.

    The key is bucketed by ``renotify_after_seconds``, so
    ``node_slack_publish_effect``'s durable ledger collapses the repeat. That
    is why this node keeps no state file of its own.
    """
    first = tuple(
        _window(i, EnumConsumerFlowState.STALLED, messages_in=1, messages_out=0)
        for i in range(policy.confirm_windows)
    )
    later = tuple(
        _window(i, EnumConsumerFlowState.STALLED, messages_in=1, messages_out=0)
        for i in range(policy.confirm_windows + 3)
    )
    key_first = decide_stall_alert(_request(first, policy)).idempotency_key
    key_later = decide_stall_alert(_request(later, policy)).idempotency_key
    assert key_first == key_later


@pytest.mark.unit
def test_two_legs_of_one_bridge_are_evaluated_independently(
    policy: ModelStallAlertPolicy,
) -> None:
    """The OMN-16754 defect must not reappear in the alerting surface.

    A single averaged verdict across two legs is what hid the forwarder's dead
    inbound leg behind its healthy outbound one.
    """
    inbound = decide_stall_alert(
        _request(
            tuple(
                _window(i, EnumConsumerFlowState.STARVED, messages_in=0, messages_out=0)
                for i in range(policy.confirm_windows)
            ),
            policy,
            consumer_group="gateway-forwarder",
            topic="onex.cmd.gateway.inbound.v1",
        )
    )
    outbound = decide_stall_alert(
        _request(
            tuple(
                _window(
                    i,
                    EnumConsumerFlowState.FLOWING,
                    messages_in=5575,
                    messages_out=5575,
                )
                for i in range(policy.confirm_windows)
            ),
            policy,
            consumer_group="gateway-forwarder",
            topic="onex.evt.gateway.outbound.v1",
        )
    )
    assert inbound.outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert outbound.outcome is EnumStallAlertOutcome.NO_ALERT


@pytest.mark.unit
def test_a_stalled_window_without_failure_evidence_is_not_a_stall(
    policy: ModelStallAlertPolicy,
) -> None:
    """AC3 — an out-count of zero is not, on its own, evidence of a dead leg.

    Two whole classes of healthy node publish nothing at all: a projection
    writes rows to Postgres, and a compute delivers its intent IN-PROCESS
    through ``IntentEffectDispatchBridge``, which is why
    ``node_gateway_link_health_projection_compute`` sits at Kafka
    high-watermark 0 while fully alive (premise falsified live, 2026-08-29).

    Measured on the .201 dev lane at 2026-08-29T02:40Z, this rule separates the
    two populations completely — every genuine failure carried
    ``messages_dlq == messages_in``; every healthy non-publishing leg carried
    ``dlq=0 errors=0``. Without it the first live dispatch posts nine alerts,
    most of them wrong.
    """
    windows = tuple(
        _window(
            index,
            EnumConsumerFlowState.STALLED,
            messages_in=3,
            messages_out=0,
            messages_dlq=0,
            handler_errors=0,
        )
        for index in range(policy.confirm_windows + policy.clear_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.NO_ALERT
    assert decision.should_publish is False
    assert decision.alert is None


@pytest.mark.unit
def test_the_same_history_alerts_once_the_dead_letters_appear(
    policy: ModelStallAlertPolicy,
) -> None:
    """The corroboration rule suppresses noise, it does not suppress the signal.

    Byte-identical history to the test above except that the windows carry the
    dead-letters a genuinely failing consumer produces — the shape this very
    node was in on the dev lane while its own dispatches were failing
    validation (``messages_dlq == messages_in``, 100% DLQ).
    """
    windows = tuple(
        _window(
            index,
            EnumConsumerFlowState.STALLED,
            messages_in=13,
            messages_out=0,
            messages_dlq=13,
            handler_errors=13,
        )
        for index in range(policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert decision.alert is not None
    assert decision.alert.messages_dlq == 13


@pytest.mark.unit
def test_starved_needs_no_second_witness(policy: ModelStallAlertPolicy) -> None:
    """STARVED is deliberately not corroboration-gated.

    ``in == 0`` while upstream is producing means the consumer is not taking
    messages at all. There is no healthy reading of that, so requiring a
    dead-letter it could not possibly produce would silence the one state that
    already proves itself.
    """
    assert EnumConsumerFlowState.STARVED not in policy.require_failure_evidence_for
    windows = tuple(
        _window(
            index,
            EnumConsumerFlowState.STARVED,
            messages_in=0,
            messages_out=0,
            messages_dlq=0,
            handler_errors=0,
        )
        for index in range(policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL


@pytest.mark.unit
def test_an_unobserved_counter_is_never_read_as_failure_evidence(
    policy: ModelStallAlertPolicy,
) -> None:
    """``None`` is the absence of an observation, not a dead-letter.

    Treating an unobserved counter as corroboration would let a runtime that
    stopped reporting manufacture a confirmed alert out of nothing — the same
    error, one layer down, that OMN-16777 AC5 closes for the window itself.
    """
    windows = tuple(
        ModelFlowWindowObservation(
            window_start=_EPOCH + index * _WINDOW,
            window_end=_EPOCH + (index + 1) * _WINDOW,
            flow_state=EnumConsumerFlowState.STALLED,
            messages_in=5,
            messages_out=0,
            messages_dlq=None,
            handler_errors=None,
        )
        for index in range(policy.confirm_windows)
    )
    decision = decide_stall_alert(_request(windows, policy))
    assert decision.outcome is EnumStallAlertOutcome.NO_ALERT
