# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input to the dev seed (OMN-19970)."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelDevSeedRequest(BaseModel):
    """Which tenant the seeded rows belong to, and the instant they are dated from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # string-id-ok: the tenant a delegate-skill terminal carries (slug or UUID text)
    tenant_id: str | None = Field(
        default=None,
        description="The machine's or lane's own registered tenant. None writes no tenant key.",
    )
    now: AwareDatetime | None = Field(
        default=None, description="Seed time; each run is dated days_ago before it."
    )

    def seed_time(self, fallback: datetime) -> datetime:
        return self.now if self.now is not None else fallback
