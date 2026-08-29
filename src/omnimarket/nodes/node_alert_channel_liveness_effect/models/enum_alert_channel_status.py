# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Four states for an alert channel, where the platform had two (OMN-15600).

Every alert path on this platform branched on whether a credential was *set*.
A credential that is set and dead was therefore indistinguishable from a
healthy one, which is how ``SLACK_WEBHOOK_URL`` delivered nothing for an
unknown number of months while every caller's ``curl`` returned 0.

The bot-token path that replaced it carries the same trap in a worse shape:
Slack answers ``HTTP 200`` with ``{"ok": false}`` for ``channel_not_found``,
``not_in_channel``, ``invalid_auth`` and ``token_revoked``.  A status-code check
scores all four as delivered.
"""

from __future__ import annotations

from enum import StrEnum


class EnumAlertChannelStatus(StrEnum):
    """Verdict for one probe of the alert delivery channel."""

    #: Credentials resolved, the token authenticated, the channel exists, and
    #: the bot is a member of it. An alert published now would arrive.
    LIVE = "LIVE"

    #: Configured and provably unable to deliver. This is the state the whole
    #: ticket exists to make expressible: the credential is present, the
    #: transport succeeds, and the alert goes nowhere.
    DEAD = "DEAD"

    #: No credential at all. Deliberately NOT folded into DEAD — the operator
    #: action differs (provision one vs. repair one), and collapsing the two
    #: loses which action to take.
    NOT_CONFIGURED = "NOT_CONFIGURED"

    #: The probe itself could not run. Never healthy. OMN-15606 is the
    #: precedent: omniclaude's ``probe_channel()`` returned ``"unknown"`` on any
    #: exception and both of its consumers treated that as not-failed, so the
    #: detector reported clean while the channel may have been dead. A detector
    #: that fails OPEN is worse than no detector, because it is trusted.
    PROBE_ERROR = "PROBE_ERROR"

    @property
    def is_healthy(self) -> bool:
        """True only for :attr:`LIVE`.

        Health is derived from the status rather than carried beside it, so
        there is no way to construct a verdict that is DEAD and healthy at the
        same time.
        """
        return self is EnumAlertChannelStatus.LIVE


__all__ = ["EnumAlertChannelStatus"]
