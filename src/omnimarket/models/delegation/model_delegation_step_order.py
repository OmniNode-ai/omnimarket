# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The engine a delegation step runs first, and where that order came from (OMN-24044)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelDelegationStepOrder(BaseModel):
    """``choice`` is the text step's first engine or the code step's GLM position; ``fields`` are receipt fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    choice: str
    order_source: Literal["flag", "routing_order_node", "default"]
    routing_order: tuple[str, ...] | None = None
    order_fallback: str | None = None

    def receipt_fields(self) -> dict[str, object]:
        """The fields the step writes onto each receipt: the source, and the order or why the default stood."""
        fields: dict[str, object] = {"order_source": self.order_source}
        if self.routing_order is not None:
            fields["routing_order"] = list(self.routing_order)
        if self.order_fallback is not None:
            fields["order_fallback"] = self.order_fallback
        return fields
