# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed settings of the decision environment node (OMN-24355)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class ModelLandingDecisionVenvOverlay(BaseModel):
    """``overlays/node_landing_decision_venv_effect/overlay.yaml`` of the overlays repository.

    Every setting the node uses (the rows from a pin key to a distribution, the interpreter arguments
    and the timeouts) is a field here; the handler holds none of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Pin key to distribution name, one row per dependency the pin names; a pin without a row's key is refused.
    distributions: dict[NonEmpty, NonEmpty] = Field(min_length=1)
    #: Arguments after the base interpreter that create the environment (its directory is appended).
    venv_args: list[NonEmpty] = Field(min_length=1)
    #: Arguments after the environment's interpreter that install (the ``name==version`` requirements are appended).
    pip_args: list[NonEmpty] = Field(min_length=1)
    venv_timeout_seconds: float = Field(gt=0)
    pip_timeout_seconds: float = Field(gt=0)
    probe_timeout_seconds: float = Field(gt=0)
