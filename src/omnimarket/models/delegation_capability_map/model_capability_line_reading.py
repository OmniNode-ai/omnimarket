# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of the capability map's class line, or the refusal naming the key.

Either the line an overlay configured, or the reason nothing configured one:
exactly one of the two is set. There is no packaged line in place of a missing
overlay value, so an absent key reads as an explicit not-configured line rather
than a refusal.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)


class ModelCapabilityLineReading(BaseModel):
    """The class line a capability map run carries, or why no overlay set it.

    ``line`` is the class line an overlay configured; ``not_configured_reason``
    names the key ``delegation.capability_map.line`` no overlay supplied. A
    present overlay that does not validate raises rather than reading as an
    absent one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: ModelCapabilityLine | None = None
    not_configured_reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.line is None) == (self.not_configured_reason is None):
            raise ValueError("exactly one of line and not_configured_reason is set")
        return self
