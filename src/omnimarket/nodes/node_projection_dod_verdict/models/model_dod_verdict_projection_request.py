# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input to the pure definition-of-done verdict fold (def-B request)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_dod_verdict.models.model_dod_verdict_wire import (
    ModelDodVerdictWire,
)


class ModelDodVerdictProjectionRequest(BaseModel):
    """One completed verification event, and nothing else.

    The fold needs no database fact: unlike a fingerprint row, a verification
    run is complete in itself and accumulates nothing across events. Replaying
    the same request therefore reproduces the same row byte for byte, which is
    what makes an acceptance criterion about the verdict falsifiable by a unit
    test rather than by a live lane.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelDodVerdictWire = Field(..., description="The arriving verdict.")


__all__ = ["ModelDodVerdictProjectionRequest"]
