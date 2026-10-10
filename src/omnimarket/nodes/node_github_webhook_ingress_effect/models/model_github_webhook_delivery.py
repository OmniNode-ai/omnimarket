# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signed GitHub webhook delivery command (OMN-19492).

The onex-api door (OMN-19592) publishes one of these per delivery GitHub posts
to ``POST /v1/github/webhook``. The body travels base64-encoded so the bytes
this node re-verifies are the bytes GitHub signed, byte for byte: re-encoding
the JSON would change whitespace and key order and break the HMAC.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelGitHubWebhookDelivery(BaseModel):
    """One GitHub webhook delivery, as received at the door."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    event: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="The X-GitHub-Event header, e.g. 'pull_request'.",
    )
    delivery_id: UUID = Field(
        ...,
        description="The X-GitHub-Delivery GUID; redeliveries reuse it.",
    )
    signature_256: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The X-Hub-Signature-256 header, 'sha256=<hex>'.",
    )
    body_b64: str = Field(
        ...,
        min_length=1,
        description="The raw request body, base64-encoded, exactly as received.",
    )
    received_at: datetime = Field(
        ..., description="When the door received the delivery (UTC)."
    )
    # The gateway forwarder's inbound leg stamps the verified tenant slug into
    # every mirrored payload (omnibase_infra.shared.tenant_stamp). It is
    # transport provenance, not part of the delivery; declared so a mirrored
    # command validates under extra="forbid".
    tenant_id: str | None = Field(
        default=None, description="Tenant slug stamped by the gateway forwarder."
    )


__all__: list[str] = ["ModelGitHubWebhookDelivery"]
