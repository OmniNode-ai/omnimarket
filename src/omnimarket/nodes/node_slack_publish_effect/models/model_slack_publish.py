# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output models for node_slack_publish_effect (OMN-13723).

``ModelSlackPublish`` is the command payload consumed from the bus.
``ModelSlackPublishResult`` is the result emitted as a terminal event.

Design invariants:
- ``channel`` is REQUIRED and has no default; the node fails closed when it is
  absent. The caller (orchestrator / skill) is responsible for supplying the
  channel from overlay config — never hardcoded in code or skill markdown.
- ``blocks`` and ``text`` are both optional; Slack requires at least one; the
  handler validates this at runtime.
- ``idempotency_key`` is caller-supplied: ``{run_date}|{channel}|{content_hash}``.
  The handler checks the durable ledger and returns ``deduped=True`` without
  posting when a prior ``slack_ts`` exists for the key.
- No Block Kit formatting occurs here; the primitive sends whatever the caller
  provides verbatim.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelSlackPublish(BaseModel):
    """Command payload for a single Slack post.

    Consumed from ``onex.cmd.omnimarket.slack-publish.v1``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(
        ...,
        description=(
            "Slack channel ID to post to (e.g. C01234567). "
            "REQUIRED — no default. Caller supplies from overlay config."
        ),
        min_length=1,
    )
    blocks: list[dict[str, Any]] | None = Field(
        default=None,
        description="Block Kit block array, passed through verbatim. Required when text is absent.",
    )
    text: str | None = Field(
        default=None,
        description=(
            "mrkdwn fallback text. Used by Slack for notifications and when blocks "
            "is absent. Required when blocks is absent."
        ),
    )
    thread_ts: str | None = Field(
        default=None,
        description="Slack message timestamp to reply into (for threading).",
    )
    idempotency_key: str = Field(
        ...,
        description=(
            "Caller-supplied deduplication key: run_date|channel|content_hash. "
            "The handler checks the durable ledger before posting; a duplicate key "
            "returns deduped=True with the prior slack_ts."
        ),
        min_length=1,
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation ID flowing through the orchestration pipeline.",
    )


class ModelSlackPublishResult(BaseModel):
    """Result of a single Slack post attempt.

    Emitted as ``onex.evt.omnimarket.slack-published.v1`` on success or
    ``onex.evt.omnimarket.slack-published.v1`` with ``success=False`` on failure.
    ``deduped=True`` is emitted as ``onex.evt.omnimarket.slack-publish-deduped.v1``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(
        ..., description="True when the message was accepted by Slack."
    )
    ts: str | None = Field(
        default=None,
        description="Slack message timestamp (slack_ts) returned by chat.postMessage.",
    )
    deduped: bool = Field(
        default=False,
        description=(
            "True when the idempotency_key matched a prior ledger entry; "
            "no POST was made and ts is the prior slack_ts."
        ),
    )
    error_code: str | None = Field(
        default=None,
        description="Slack API error code or transport error code on failure.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation ID from the input command.",
    )


__all__: list[str] = ["ModelSlackPublish", "ModelSlackPublishResult"]
