# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn a read-only observation into a verdict. Pure; no I/O (OMN-15600).

The order of the branches below is the substance of this module, so it is
stated once here rather than rediscovered from the code:

1. **The probe's own failure comes first.**  A transport error is not evidence
   about the channel in either direction.  It is PROBE_ERROR, and PROBE_ERROR
   is not healthy.  OMN-15606 is the precedent for the alternative: omniclaude's
   ``probe_channel()`` swallowed every exception into ``"unknown"``, and both
   of its consumers treated ``"unknown"`` as not-failed, so the detector
   reported clean while the channel may have been dead.
2. **Absent credentials are their own state.**  NOT_CONFIGURED, never DEAD.
   The repair differs and collapsing them loses which one to make.
3. **``ok`` decides, never the HTTP status.**  Slack answers ``200`` with
   ``{"ok": false}`` for ``invalid_auth``, ``token_revoked``,
   ``channel_not_found`` and ``not_in_channel``.  A status-code check scores
   all four as delivered — which is the bot-token path's version of the exact
   trap the dead ``SLACK_WEBHOOK_URL`` set with its ``404``.
4. **Membership is part of deliverability.**  A bot outside the channel gets
   ``ok:true`` from ``auth.test`` and ``ok:true`` from ``conversations.info``,
   then loses the alert to ``ok:false`` at ``chat.postMessage``.  Two green
   preliminaries and a dead channel.
5. **The default branch is PROBE_ERROR, not LIVE.**  LIVE is reachable only by
   an explicit conjunction of positive observations.  Nothing falls through to
   healthy.
"""

from __future__ import annotations

from omnimarket.nodes.node_alert_channel_liveness_effect.models.enum_alert_channel_status import (
    EnumAlertChannelStatus,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.models.model_alert_channel_liveness import (
    ModelAlertChannelObservation,
    ModelAlertChannelVerdict,
)

#: Used when Slack answers ``ok:false`` without naming an error, so a DEAD
#: verdict always carries a code a reader can act on.
_UNNAMED_SLACK_ERROR = "unknown_error"

#: The error ``chat.postMessage`` would return for a channel the bot can see
#: but is not a member of. Recorded on the verdict so the DEAD state names the
#: failure the next real alert would have hit.
_NOT_IN_CHANNEL = "not_in_channel"


def classify_channel_probe(
    observation: ModelAlertChannelObservation,
) -> ModelAlertChannelVerdict:
    """Return the alert channel's state from one read-only observation.

    Args:
        observation: The raw answers the probe collected, with no judgement
            applied. Tri-state throughout: ``None`` means *not observed* and is
            never read as ``False``.

    Returns:
        The verdict. ``LIVE`` only when the token authenticated, the channel
        resolved, and the bot is a member of it — every other combination is a
        named non-healthy state.
    """
    if observation.transport_error is not None:
        return ModelAlertChannelVerdict(
            status=EnumAlertChannelStatus.PROBE_ERROR,
            reason=(
                "the liveness probe could not complete, so the channel is "
                "neither proven alive nor proven dead: "
                f"{observation.transport_error}"
            ),
        )

    if not observation.credentials_present:
        return ModelAlertChannelVerdict(
            status=EnumAlertChannelStatus.NOT_CONFIGURED,
            reason=(
                "no alert channel credentials resolved; there is nothing to "
                "deliver through and nothing to repair — one has to be "
                "provisioned"
            ),
        )

    if observation.auth_ok is False:
        error = observation.auth_error or _UNNAMED_SLACK_ERROR
        return ModelAlertChannelVerdict(
            status=EnumAlertChannelStatus.DEAD,
            reason=(
                "the bot token is configured and does not authenticate "
                f"(auth.test returned ok=false, error={error}); every alert "
                "published through it is lost"
            ),
            slack_error=error,
        )

    if observation.channel_ok is False:
        error = observation.channel_error or _UNNAMED_SLACK_ERROR
        return ModelAlertChannelVerdict(
            status=EnumAlertChannelStatus.DEAD,
            reason=(
                "the token authenticates but the destination channel does not "
                f"resolve (conversations.info returned ok=false, error={error})"
            ),
            slack_error=error,
        )

    if observation.auth_ok is True and observation.channel_ok is True:
        if observation.bot_is_member is False:
            return ModelAlertChannelVerdict(
                status=EnumAlertChannelStatus.DEAD,
                reason=(
                    "the token authenticates and the channel exists, but the "
                    "bot is not a member of it: chat.postMessage would answer "
                    "HTTP 200 with ok=false and the alert would be lost with "
                    "every preliminary check green"
                ),
                slack_error=_NOT_IN_CHANNEL,
            )
        if observation.bot_is_member is True:
            return ModelAlertChannelVerdict(
                status=EnumAlertChannelStatus.LIVE,
                reason=(
                    "the token authenticated, the channel resolved, and the "
                    "bot is a member of it"
                ),
            )

    return ModelAlertChannelVerdict(
        status=EnumAlertChannelStatus.PROBE_ERROR,
        reason=(
            "the probe returned an incomplete observation "
            f"(auth_ok={observation.auth_ok}, "
            f"channel_ok={observation.channel_ok}, "
            f"bot_is_member={observation.bot_is_member}); an unanswered check "
            "is not a passed check"
        ),
    )


__all__ = ["classify_channel_probe"]
