# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed payloads for the alert-channel liveness check (OMN-15600).

Four models, one per seam:

``ModelAlertChannelProbeTrigger``
    What the node is *sent* — a runtime heartbeat.  Carries nothing the verdict
    depends on, deliberately.

``ModelAlertChannelObservation``
    What the probe *saw* — the raw read-only answers, before any judgement.
    Separating observation from verdict is what lets the classifier be a pure
    function that the hermetic tests drive directly.

``ModelAlertChannelVerdict``
    What the classifier *decided*.

``ModelAlertChannelLivenessResult``
    What leaves the node on its terminal event.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_alert_channel_liveness_effect.models.enum_alert_channel_status import (
    EnumAlertChannelStatus,
)


class ModelAlertChannelProbeTrigger(BaseModel):
    """The runtime heartbeat, as this node's declared ``input_model``.

    ``extra="ignore"`` because this is validated against the WHOLE
    ``onex.evt.platform.node-heartbeat.v1`` payload — uptime, memory, flow
    window and the rest are none of this node's business, and forbidding them
    would reject every heartbeat.

    Every field is optional, and that is the point rather than an oversight.
    The verdict depends on Slack, not on the tick: whether the alert channel can
    receive an alert must not become contingent on the shape of an unrelated
    payload.  OMN-16778 is the fresh precedent for what that coupling costs —
    the stall alert declared a shape no producer emits and DLQ'd 94 messages in
    two minutes without ever reaching its own logic.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_name: str | None = Field(
        default=None,
        description="The heartbeating service, recorded for correlation only.",
    )
    node_id: str | None = Field(
        default=None,
        description="The heartbeating node, recorded for correlation only.",
    )


class ModelAlertChannelObservation(BaseModel):
    """What the read-only probe saw, with no judgement applied yet.

    Every answer is tri-state.  ``None`` means *not observed* — the call was
    never made, or its body could not be read — and is never collapsed into
    ``False``.  The distinction is the whole reason PROBE_ERROR exists as a
    state separate from DEAD.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    credentials_present: bool = Field(
        ...,
        description=(
            "Both the bot token ref and the channel ref resolved to a value. "
            "False is NOT_CONFIGURED and is not a failure of the channel."
        ),
    )
    auth_ok: bool | None = Field(
        default=None,
        description=(
            "The ``ok`` field of the auth.test response body — never its HTTP "
            "status. Slack returns 200 with ok=false for a revoked token."
        ),
    )
    auth_error: str | None = Field(
        default=None,
        description="The ``error`` field Slack returned alongside ok=false.",
    )
    channel_ok: bool | None = Field(
        default=None,
        description=(
            "The ``ok`` field of the conversations.info response body. False "
            "for channel_not_found and for a channel the token cannot see."
        ),
    )
    channel_error: str | None = Field(
        default=None,
        description="The ``error`` field Slack returned alongside ok=false.",
    )
    bot_is_member: bool | None = Field(
        default=None,
        description=(
            "``channel.is_member``. A bot outside the channel gets ok=true from "
            "both probes and ok=false from chat.postMessage — the alert is lost "
            "with every preliminary check green."
        ),
    )
    transport_error: str | None = Field(
        default=None,
        description=(
            "The probe could not complete: DNS, TLS, timeout, unreadable body. "
            "Not evidence about the channel, and never evidence of health."
        ),
    )


class ModelAlertChannelVerdict(BaseModel):
    """The classified state of the alert channel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumAlertChannelStatus
    reason: str = Field(
        ...,
        min_length=1,
        description="Why this status, in the words an operator would need.",
    )
    slack_error: str | None = Field(
        default=None,
        description=(
            "The Slack error code that produced a DEAD verdict, carried "
            "verbatim so a reader does not have to parse the reason text."
        ),
    )

    @property
    def healthy(self) -> bool:
        """Derived from the status; never independently assignable."""
        return self.status.is_healthy


class ModelAlertChannelLivenessResult(BaseModel):
    """What the node returns, and therefore what its terminal event carries.

    The runtime publishes this to
    ``onex.evt.omnimarket.alert-channel-liveness-checked.v1``.  That topic is a
    surface *independent of the channel being judged*: a Slack outage cannot
    suppress the report that Slack is down, which is the circularity that made
    the original failure invisible ("the only thing that would tell you
    alerting is broken is the alerting").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    probed: bool = Field(
        ...,
        description=(
            "False when this tick fell inside the interval already proven by "
            "the previous probe. Not a failure and not a result."
        ),
    )
    verdict: ModelAlertChannelVerdict | None = Field(
        default=None,
        description="Present exactly when ``probed`` is true.",
    )
    probe_interval_seconds: int = Field(
        ...,
        gt=0,
        description="The contract-declared interval this node throttled to.",
    )
    failure_surfaced: bool = Field(
        ...,
        description=(
            "A non-LIVE verdict was recorded on this event and logged at ERROR. "
            "The ticket's AC2 in one boolean: the outcome was not discarded."
        ),
    )

    @property
    def healthy(self) -> bool | None:
        """``None`` when this tick was throttled — unknown, not healthy."""
        return None if self.verdict is None else self.verdict.healthy


__all__ = [
    "ModelAlertChannelLivenessResult",
    "ModelAlertChannelObservation",
    "ModelAlertChannelProbeTrigger",
    "ModelAlertChannelVerdict",
]
