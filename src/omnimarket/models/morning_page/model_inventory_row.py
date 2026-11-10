# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One line of the status page exposure census."""

from pydantic import BaseModel, ConfigDict


class ModelInventoryRow(BaseModel):
    """One line of the exposure census — what the serving path would answer."""

    model_config = ConfigDict(frozen=True)

    topic: str
    backing: str
    source_contract: str
    cached_row_count: int
    latest_event_at: str | None
    tenant_scoped: bool
