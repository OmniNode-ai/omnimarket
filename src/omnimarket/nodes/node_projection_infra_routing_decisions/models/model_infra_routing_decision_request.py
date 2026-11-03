# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's input: one delivery from the routing-decision topic."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelInfraRoutingDecisionProjectionRequest(BaseModel):
    """One delivery, either the envelope or the decision it carries.

    The delivery is held as a mapping because the topic carries both shapes: the
    ``ModelEventEnvelope`` the delegation routing reducer publishes (decision
    under ``payload``), and the bare decision the runtime hands an in-process
    writer once it has unwrapped that envelope. The fold owns the unwrap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    delivery: dict[str, object] = Field(
        description="The decoded delivery with the runtime's underscore keys removed.",
    )


__all__: list[str] = ["ModelInfraRoutingDecisionProjectionRequest"]
