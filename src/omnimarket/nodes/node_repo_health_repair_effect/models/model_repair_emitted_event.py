# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output event model for node_repo_health_repair_effect.

Published on ``onex.evt.omnimarket.repo-health-repair-emitted.v1`` after a
durable repair task has been emitted (or idempotently identified as already
existing).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelRepoHealthRepairEmittedEvent(BaseModel):
    """Durable repair task emitted (or idempotently found) event.

    The content_key is the SHA-256 hex digest of the canonical dedup string
    (failing_command + sorted failing_paths). Two identical inputs produce the
    same key; if a ticket with this key already exists, ticket_created is False
    and repair_ticket_ref carries the pre-existing reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID carried from the command."
    )
    repo: str = Field(
        ..., description="Repository slug from the failure classification."
    )
    pr_number: int | None = Field(
        default=None, description="PR number from the failure classification."
    )
    failing_command: str = Field(
        ..., description="The failing command from the classification."
    )
    classification_reason: str = Field(
        ...,
        description="Human-readable evidence from the REPO_BASELINE classification.",
    )
    content_key: str = Field(
        ...,
        description=(
            "SHA-256 hex digest of 'failing_command + sorted(failing_paths)' — "
            "the idempotency key that prevents duplicate tickets across sweep runs."
        ),
    )
    repair_ticket_ref: str | None = Field(
        default=None,
        description=(
            "Linear ticket identifier (e.g. 'OMN-13999') created or found for this "
            "repair task. None in dry_run mode."
        ),
    )
    ticket_created: bool = Field(
        ...,
        description=(
            "True when a new ticket was created; False when an existing ticket was "
            "found via the content_key (idempotent re-run) or dry_run=True."
        ),
    )
    dry_run: bool = Field(
        ...,
        description="Mirrors the dry_run flag from the inbound command.",
    )


__all__ = ["ModelRepoHealthRepairEmittedEvent"]
