# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether one consumer's flow history deserves an alert. Pure; no I/O.

OMN-16778, Phase 1 of epic OMN-16776.

``node_projection_consumer_flow`` (OMN-16777) made a stalled consumer visible.
A projection nobody reads is not observability: OMN-14440 fired every 30
minutes for three months into a log nobody opened, and on 2026-08-27 the
delegation chain died, quarantined 16 commands in about a minute, and nothing
fired at all.  This handler is the half that speaks.

Canonical definition-B shape
----------------------------
``handle(request: ModelConsumerFlowStallAlertRequest) ->
ModelConsumerFlowStallAlertDecision``: typed payload in, typed payload out.  No
event envelope in the core, no clock, no database, no Slack client.  The window
history and the contract-declared policy both arrive as input, so the same
history always yields the same decision and the hermetic tests drive this exact
function rather than a stand-in.

Why hysteresis, and why it is asymmetric
----------------------------------------
``confirm_windows`` before firing and a strictly larger ``clear_windows``
before calling it recovered is the damping already proven on the ``.201`` host
reporter (OMN-16789), which before it had that damping produced the alert flap
the operator asked to have stopped.  Firing on a single window would reproduce
it here, and an alert channel that flaps is muted within a day -- at which
point this node is worth exactly as much as the silence it replaced.

Why ``UNKNOWN`` never fires
---------------------------
A missed heartbeat is the absence of an observation, not evidence of a stall
(OMN-16777 AC5).  It breaks an alerting run rather than extending it, and it
surfaces as its own ``WARN_MISSED_WINDOW`` outcome so it is neither silently
healthy nor silently stalled.

Why there is no threshold in this file
--------------------------------------
Every number lives in ``contract.yaml`` under ``alert_policy`` and reaches this
function inside ``request.policy``.  AC2 is falsified by any threshold literal
in a ``.py`` file, and ``test_no_threshold_literal_lives_in_python`` asserts
that mechanically rather than on trust.
"""

from __future__ import annotations

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    EnumStallAlertOutcome,
    EnumStallAlertSeverity,
    ModelConsumerFlowStallAlertDecision,
    ModelConsumerFlowStallAlertRequest,
    ModelFlowWindowObservation,
    ModelStallAlertPayload,
    ModelStallAlertPolicy,
)


def _trailing_run(
    windows: tuple[ModelFlowWindowObservation, ...],
    policy: ModelStallAlertPolicy,
) -> int:
    """Length of the alerting run ending at the newest window.

    Stops at the first window that is not an alerting state -- including
    ``UNKNOWN``, which is deliberately not treated as a continuation.  An
    unobserved window is not evidence that the stall persisted through it, and
    treating it as evidence would let a runtime that stopped heartbeating
    manufacture a confirmed alert out of nothing.
    """
    run = 0
    for window in reversed(windows):
        if not policy.is_alerting(window.flow_state):
            break
        run += 1
    return run


def _trailing_unknown_run(windows: tuple[ModelFlowWindowObservation, ...]) -> int:
    """Length of the ``UNKNOWN`` run ending at the newest window."""
    run = 0
    for window in reversed(windows):
        if window.flow_state is not EnumConsumerFlowState.UNKNOWN:
            break
        run += 1
    return run


def _windows_since_last_alerting(
    windows: tuple[ModelFlowWindowObservation, ...],
    policy: ModelStallAlertPolicy,
) -> int | None:
    """How many windows have elapsed since the last alerting one.

    ``None`` when the history contains no alerting window at all -- which is
    the ordinary case for a healthy or genuinely idle consumer and must not be
    confused with "recovered a very long time ago".
    """
    for offset, window in enumerate(reversed(windows)):
        if policy.is_alerting(window.flow_state):
            return offset
    return None


def _renotify_bucket(window_start_epoch: int, renotify_after_seconds: int) -> int:
    """Bucket index used to collapse repeats of a standing condition.

    The alert's idempotency key carries this bucket, so a condition that stays
    broken re-posts at most once per declared period through
    ``node_slack_publish_effect``'s existing durable ledger.  No state file, no
    second dedupe mechanism, and nothing this node has to remember between
    invocations.
    """
    return window_start_epoch // renotify_after_seconds


class HandlerConsumerFlowStallAlert:
    """Classify a consumer's trailing flow history against the declared policy."""

    def handle(
        self, request: ModelConsumerFlowStallAlertRequest
    ) -> ModelConsumerFlowStallAlertDecision:
        """Return the alerting decision for one (consumer_group, topic).

        Args:
            request: The trailing window history (oldest first) and the
                contract-declared policy to judge it by.

        Returns:
            The outcome, its severity, whether anything should be published,
            and -- exactly when something should be -- the payload naming the
            consumer, the topic, the counters and the run length.
        """
        policy = request.policy
        windows = request.windows
        newest = windows[-1]

        alerting_run = _trailing_run(windows, policy)
        unknown_run = _trailing_unknown_run(windows)

        if alerting_run >= policy.confirm_windows:
            payload = ModelStallAlertPayload(
                consumer_group=request.consumer_group,
                topic=request.topic,
                flow_state=newest.flow_state,
                consecutive_windows=alerting_run,
                messages_in=newest.messages_in,
                messages_out=newest.messages_out,
                messages_dlq=newest.messages_dlq,
                handler_errors=newest.handler_errors,
                window_start=newest.window_start,
                window_end=newest.window_end,
                node_id=request.node_id,
                correlation_id=request.correlation_id,
            )
            bucket = _renotify_bucket(
                int(newest.window_start.timestamp()),
                policy.renotify_after_seconds,
            )
            return ModelConsumerFlowStallAlertDecision(
                consumer_group=request.consumer_group,
                topic=request.topic,
                outcome=EnumStallAlertOutcome.FAIL_CONFIRMED_STALL,
                severity=EnumStallAlertSeverity.FAIL,
                consecutive_alerting_windows=alerting_run,
                consecutive_unknown_windows=unknown_run,
                should_publish=True,
                reason=(
                    f"{request.consumer_group} has been "
                    f"{newest.flow_state.value} on {request.topic} for "
                    f"{alerting_run} consecutive windows "
                    f"(confirm threshold {policy.confirm_windows})"
                ),
                idempotency_key=(
                    f"{bucket}|{request.consumer_group}|{request.topic}|"
                    f"{newest.flow_state.value}"
                ),
                alert=payload,
            )

        if alerting_run > 0:
            return ModelConsumerFlowStallAlertDecision(
                consumer_group=request.consumer_group,
                topic=request.topic,
                outcome=EnumStallAlertOutcome.PENDING_CONFIRMATION,
                severity=EnumStallAlertSeverity.WARN,
                consecutive_alerting_windows=alerting_run,
                consecutive_unknown_windows=unknown_run,
                should_publish=policy.deliver_warnings,
                reason=(
                    f"{request.consumer_group} is {newest.flow_state.value} on "
                    f"{request.topic} for {alerting_run} window(s), under the "
                    f"declared confirm threshold of {policy.confirm_windows}"
                ),
            )

        if unknown_run >= policy.unknown_warn_windows:
            return ModelConsumerFlowStallAlertDecision(
                consumer_group=request.consumer_group,
                topic=request.topic,
                outcome=EnumStallAlertOutcome.WARN_MISSED_WINDOW,
                severity=EnumStallAlertSeverity.WARN,
                consecutive_alerting_windows=0,
                consecutive_unknown_windows=unknown_run,
                should_publish=policy.deliver_warnings,
                reason=(
                    f"{request.consumer_group} has {unknown_run} unobserved "
                    f"window(s) on {request.topic}; a missed heartbeat is not "
                    "evidence of a stall and not evidence of health"
                ),
            )

        since_alerting = _windows_since_last_alerting(windows, policy)
        if since_alerting is not None and since_alerting < policy.clear_windows:
            return ModelConsumerFlowStallAlertDecision(
                consumer_group=request.consumer_group,
                topic=request.topic,
                outcome=EnumStallAlertOutcome.RECOVERING,
                severity=EnumStallAlertSeverity.WARN,
                consecutive_alerting_windows=0,
                consecutive_unknown_windows=unknown_run,
                should_publish=False,
                reason=(
                    f"{request.consumer_group} has been clear on "
                    f"{request.topic} for {since_alerting} window(s), under "
                    f"the declared clear threshold of {policy.clear_windows}"
                ),
            )

        return ModelConsumerFlowStallAlertDecision(
            consumer_group=request.consumer_group,
            topic=request.topic,
            outcome=EnumStallAlertOutcome.NO_ALERT,
            severity=EnumStallAlertSeverity.NONE,
            consecutive_alerting_windows=0,
            consecutive_unknown_windows=unknown_run,
            should_publish=False,
            reason=(
                f"{request.consumer_group} is {newest.flow_state.value} on "
                f"{request.topic}; nothing to report"
            ),
        )


__all__ = ["HandlerConsumerFlowStallAlert"]
