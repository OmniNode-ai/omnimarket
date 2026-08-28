# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire mirror of the Slack publish command this node emits (OMN-16778).

The consuming model is
``omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish.ModelSlackPublish``.
This is a field-for-field mirror rather than a direct import for the reason
this repo states plainly in ``CLAUDE.md``: *do not make one node import another
node's private handler or model package.*  The command travels over
``onex.cmd.omnimarket.slack-publish.v1`` -- a declared topic, not a Python
call -- so the wire shape is the contract between the two nodes, and mirroring
it keeps that boundary honest instead of turning a topic into an import edge.

The mirror is ``extra="forbid"``, not ``extra="ignore"``.  OMN-14490/OMN-14506
recorded what slim ``extra="ignore"`` copies cost the registration projection:
every field they did not declare was dropped in silence.  Drift is caught
instead by ``test_omn16778_slack_command_is_accepted_by_the_publish_node``,
which round-trips this payload through the real ``ModelSlackPublish`` and fails
the moment the two shapes diverge.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelStallAlertSlackCommand(BaseModel):
    """One ``onex.cmd.omnimarket.slack-publish.v1`` command, as this node emits it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(
        ...,
        min_length=1,
        description=(
            "Slack channel id, resolved at the effect boundary from the "
            "contract's SLACK_CHANNEL_ID secret ref. Never a literal."
        ),
    )
    text: str = Field(
        ...,
        min_length=1,
        description="mrkdwn alert body naming the consumer, topic and counters.",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description=(
            "Renotify-bucketed key. The publish node's durable ledger collapses "
            "a repeat inside the same bucket."
        ),
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation context carried through the chain.",
    )


__all__ = ["ModelStallAlertSlackCommand"]
