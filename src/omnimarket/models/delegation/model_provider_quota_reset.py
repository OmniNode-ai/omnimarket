# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Internal request/result for the delegation effect's shared quota-reset handler."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelProviderQuotaResetRequest(BaseModel):
    """The policy-selected reset surface and the provider's observed response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reset_from: str | None
    reference: datetime
    reset_utc_offset_minutes: int = (
        0  # the offset the provider's naive reset stamp is written in
    )
    message: str = ""
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class ModelProviderQuotaResetResult(BaseModel):
    """A parsed reset; None leaves the bounded fallback to the policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reset_at: datetime | None = None
