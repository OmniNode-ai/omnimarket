# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_dropped_work_audit_scheduled_effect (OMN-26205): its cadence, its host and its routes.

No key has a default. The kinds, windows and delays are the compute node's own overlay, named by ``rules_overlay_id``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
    ModelLedgerAppendRoute,
)


class ModelCommandRoute(BaseModel):
    """A command the overlay names by the environment variable that holds it; unset refuses naming the variable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelDroppedWorkAuditScheduleOverlay(ModelScheduleDeclaration):
    """Cadence and host of the audit, the compute overlay it runs, and where it reads and writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules_overlay_id: str = Field(min_length=1)
    schedule_host: str = Field(min_length=1)
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    ledger_append: ModelLedgerAppendRoute
    pr_state_file_env: str = Field(min_length=1)
    merged_prs_key: str = Field(min_length=1)
    merged_at_key: str = Field(min_length=1)
    admission_refusal_read: ModelCommandRoute | None


__all__: list[str] = ["ModelCommandRoute", "ModelDroppedWorkAuditScheduleOverlay"]
