# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a stall decision as the canonical Slack publish command (OMN-16778).

Delivery is not reinvented here.  OMN-13733 consolidated every alert caller
onto ``node_slack_publish_effect``, which posts to
``https://slack.com/api/chat.postMessage`` with a bot token resolved from its
own contract secret ref.  This module only builds the command payload that node
already accepts; it opens no socket, reads no environment variable and holds no
token.

``SLACK_WEBHOOK_URL`` is deliberately absent.  Re-probed 2026-08-27 on both
``~/.omnibase/.env`` and ``.201:/data/omninode/omnibase_infra/.env``, it returns
``HTTP 404 / no_service`` and is being retired under OMN-15600.  The bot-token
path is the one that is live today -- the ``#omninode-notifications`` host-health
posts arrive on it every 15 minutes.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    ModelStallAlertPayload,
    ModelStallAlertSlackCommand,
)


def _counter(value: int | None) -> str:
    """Render a counter without inventing a zero for an unobserved window."""
    return "unobserved" if value is None else str(value)


def render_alert_text(payload: ModelStallAlertPayload) -> str:
    """One line a human can act on without opening a terminal.

    AC4 is falsified by a message that requires someone to go run ``rpk`` to
    find out what broke, so every fact the triage needs is in the text: which
    consumer, which topic, what went in, what came out, what was dead-lettered,
    and how long it has been that way.
    """
    return (
        f":rotating_light: *{payload.flow_state.value}* — "
        f"`{payload.consumer_group}` on `{payload.topic}` for "
        f"{payload.consecutive_windows} consecutive windows\n"
        f"in={_counter(payload.messages_in)} "
        f"out={_counter(payload.messages_out)} "
        f"dlq={_counter(payload.messages_dlq)} "
        f"handler_errors={_counter(payload.handler_errors)}\n"
        f"window {payload.window_start.isoformat()} → "
        f"{payload.window_end.isoformat()}\n"
        f"correlation_id={payload.correlation_id}"
    )


def build_slack_command(
    *,
    payload: ModelStallAlertPayload,
    channel: str,
    idempotency_key: str,
    correlation_id: UUID,
) -> ModelStallAlertSlackCommand:
    """Build the ``onex.cmd.omnimarket.slack-publish.v1`` command for one alert.

    Args:
        payload: The decided alert.
        channel: Slack channel id, resolved at the effect boundary from the
            contract's ``SLACK_CHANNEL_ID`` secret ref. Never a literal here.
        idempotency_key: The renotify-bucketed key from the decision. The
            publish node's durable ledger collapses a repeat inside the same
            bucket, which is what keeps a standing stall from re-posting on
            every heartbeat.
        correlation_id: Correlation context carried through the chain.

    Returns:
        The command payload, ready to publish.
    """
    return ModelStallAlertSlackCommand(
        channel=channel,
        text=render_alert_text(payload),
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


__all__ = ["build_slack_command", "render_alert_text"]
