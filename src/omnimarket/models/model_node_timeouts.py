# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The timeouts, intervals, delays and TTLs a node declares in its ``contract.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelNodeTimeouts(BaseModel):
    """Named durations in seconds, validated once when the node is composed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seconds: dict[str, float] = Field(
        ...,
        description="Duration in seconds by the name the node's contract declares.",
    )

    def get(self, name: str) -> float:
        """The declared duration; a name the contract does not declare stops the node."""
        try:
            return self.seconds[name]
        except KeyError:
            raise KeyError(
                f"contract declares no timeout named {name!r}; "
                f"declared: {sorted(self.seconds)}"
            ) from None


__all__ = ["ModelNodeTimeouts"]
