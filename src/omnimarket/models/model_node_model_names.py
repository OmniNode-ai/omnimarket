# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model names a node declares in its ``contract.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelNodeModelNames(BaseModel):
    """Model names by the name the node's contract declares, validated once at composition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    names: dict[str, str] = Field(
        ...,
        description="Model name or model key by the name the node's contract declares.",
    )

    def get(self, name: str) -> str:
        """The declared model name; a name the contract does not declare stops the node."""
        try:
            return self.names[name]
        except KeyError:
            raise KeyError(
                f"contract declares no model name {name!r}; "
                f"declared: {sorted(self.names)}"
            ) from None


__all__ = ["ModelNodeModelNames"]
