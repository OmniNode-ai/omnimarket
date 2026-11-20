# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the delegation step order (OMN-24507).

``code_engine_order`` is the overlay value the eval changes to reorder code
engines; lab first then GLM is the deployed value. ``engine_programs`` names
the program each engine needs on the host's PATH (``None`` when it needs
none), and must name both engines so a host can say which it lacks. Nothing
here has a default for an overlay key: an install with no overlay keeps the
step's old default, and an overlay that is present but invalid raises, never
a default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DELEGATION_STEP_ORDER_OVERLAY_ID = "delegation_step_order"


class ModelDelegationStepOrderOverlay(BaseModel):
    """The overlay of delegation_step_order: the code engine order and each engine's program.

    ``code_engine_order`` names at least one engine and no engine twice;
    ``engine_programs`` names the program both engines need on the host, or
    ``None`` for the engine that needs none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_engine_order: tuple[Literal["lab", "glm"], ...] = Field(min_length=1)
    engine_programs: dict[Literal["lab", "glm"], str | None]

    @model_validator(mode="after")
    def _no_engine_twice(self) -> ModelDelegationStepOrderOverlay:
        """The order places no engine twice."""
        if len(set(self.code_engine_order)) != len(self.code_engine_order):
            raise ValueError(
                f"code_engine_order places an engine twice: {list(self.code_engine_order)}"
            )
        return self

    @model_validator(mode="after")
    def _names_both_engines(self) -> ModelDelegationStepOrderOverlay:
        """``engine_programs`` names both engines."""
        missing = sorted({"lab", "glm"} - set(self.engine_programs))
        if missing:
            raise ValueError(
                f"engine_programs must name both engines; missing {missing}"
            )
        return self
