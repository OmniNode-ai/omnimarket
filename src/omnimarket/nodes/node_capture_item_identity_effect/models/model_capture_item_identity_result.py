# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One result from node_capture_item_identity_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelCaptureItemIdentityResult(BaseModel):
    """The id, number or stamped records the requested operation produced.

    ``payload`` is the stamped lineage event (stamp_lineage_event); ``records``
    are the stamped content records (stamp_content_records) or the barriers
    (session_barrier).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID | None = None
    content_record_id: UUID | None = None
    sequence: int | None = None
    payload: dict[str, JsonValue] | None = None
    records: list[dict[str, JsonValue]] | None = None


__all__ = ["ModelCaptureItemIdentityResult"]
