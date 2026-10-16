# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One request to node_capture_item_identity_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.nodes.node_capture_item_identity_effect.models.enum_capture_item_operation import (
    EnumCaptureItemOperation,
)


class ModelCaptureItemIdentityRequest(BaseModel):
    """The fields each operation reads; the others stay unset.

    - content_record_id: event_id and field.
    - claim_event_id: item_dir, session_id, agent_id, hook_event_name,
      discriminator, stream and minted_event_id.
    - next_producer_seq, high_water: item_dir, session_id and stream.
    - stamp_lineage_event: journal_dir, payload and hook_input.
    - stamp_content_records: journal_dir, session_id, agent_id, hook_input and
      records.
    - session_barrier: journal_dir, session_id, streams,
      content_capture_enabled, barrier_id and emitted_at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumCaptureItemOperation
    item_dir: str | None = None
    session_id: str | None = None
    stream: str | None = None
    agent_id: str | None = None
    hook_event_name: str | None = None
    discriminator: str | None = None
    minted_event_id: UUID | None = None
    event_id: UUID | None = None
    field: str | None = None
    journal_dir: str | None = None
    hook_input: dict[str, JsonValue] | None = None
    payload: dict[str, JsonValue] | None = None
    records: list[dict[str, JsonValue]] | None = None
    streams: list[str] | None = None
    content_capture_enabled: bool | None = None
    barrier_id: str | None = None
    emitted_at: str | None = None


__all__ = ["ModelCaptureItemIdentityRequest"]
